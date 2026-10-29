# ADR-0003: The benchmark searches rationale text, and scores one unit per change

- **Status:** Accepted (2026-10-27) — committed before the first generated golden record exists
- **Date:** 2026-10-27
- **Refines:** [ADR-0002](ADR-0002-golden-set-construction.md), which fixes where the anchor
  symbol comes from but never fixed what the benchmark searches
- **Applies to:** checkpoint B — `provenance/eval/`, `scripts/leakage_probe.py`, the golden-set
  generator, migration `0007`

## Context

Checkpoint B's benchmark asks "why is this code the way it is?" about a symbol that a commit
really changed. Three facts, each measured at corpus `1e2ad803f7`, made the existing harness
unable to support that question:

1. **ADR-0002's reassuring number and the harness search two different pools.** ADR-0002
   measured grep recall@10 = 0.175 for code-diff anchors over "the in-scope commit-message pool".
   The harness — `scripts/leakage_probe.py:250` (`load_pool_from_db`) and
   `provenance/eval/anchors.py` — searches **code chunks**. A question that names a symbol is
   answered by grep at rank 1 on a code pool, because the symbol's own chunk contains its name.
   The leakage filter would discard nearly every question and the gate would read INCONCLUSIVE.
2. **Recall was a fraction of documents, not of answers.** `metrics.recall_at_k` scores
   `|top-k ∩ required chunks| / |required chunks|`. Once a PR's description and its review
   comments are documents, one PR can own dozens of them, and retrieving one scores 1/30.
   Review finding 7 (task 3) already named the fix: score relevance *units*.
3. **The rationale already exists in the graph.** Checkpoint A stored the message of every
   first-parent commit. 6,810 of them touch an in-scope file
   (`SELECT count(*) FROM commits c WHERE EXISTS (SELECT 1 FROM file_revisions r WHERE
   r.commit_sha = c.commit_sha)`), and they reference 6,440 distinct PR numbers. Before 2020
   Airflow's issues lived in JIRA (1,081 in-scope commits cite `AIRFLOW-NNNN`), so PR→issue links
   exist only from about 2020 on.

## Decision

### 1. The pool is rationale text, stored as chunks

Every retrievable document is a row in `chunks`, so the BM25 index, the anchor resolver, the
leakage probe and the dense tables keep working unchanged.

| Document | `kind` | `path` | Arrives |
|---|---|---|---|
| Commit message (subject + body) | `commit` | `commit/<sha>` | slice B2, from checkpoint A's graph |
| PR description | `pull_request` | `pull/<n>` | slice B4 (GitHub backfill) |
| Review comment | `review_comment` | `pull/<n>/review/<id>` | slice B4 |
| Issue body / comment | `issue` / `issue_comment` | `issue/<n>`, `issue/<n>/comment/<id>` | slice B4 |

`language = 'text'`, `qualified_name = NULL`, lines `1..n`. The pool is the documents of the
**6,810 commits that touch an in-scope file**, plus — after B4 — the PRs they reference.
Mechanical commits and merges stay in the pool as realistic distractors; they are never gold.

### 2. Evidence links say what each document is

`chunk_evidence` gains two `source` values in migration `0007`:

- **`self`** — the document *is* that artifact's own text (a commit message for its commit).
- **`linked`** — the document belongs to an artifact joined by an edge: a commit message whose
  subject carries `(#N)` is also evidence for PR N; a PR's description and review comments are
  also evidence for the commit(s) that landed it.

Every document of one change is therefore evidence for **all** of that change's keys.

### 3. Questions come from an AST diff, in two templates

For each non-mechanical, non-merge commit, each in-scope file revision is diffed with the
existing tree-sitter chunker (the before and after blobs, read with `git show`):

| Template | Query class | Candidate | Gold |
|---|---|---|---|
| ``Why was `X` added to `path`?`` | `provenance` | a function or method present after, absent before | the introducing commit + its PR |
| ``Why did `X` change in <Month YYYY>?`` | `archaeology` | present on both sides, and its comment- and layout-blind token hash (`normalize.hash_ladder`) differs | that commit + its PR |

Rules that keep every question answerable and unambiguous:

- `X` must still exist at HEAD under the same qualified name, in the same rename-aware file, so
  the question is askable from today's code. `path` is today's path.
- An "added" candidate is dropped when a symbol with the same short name disappears from another
  in-scope file in the same commit: that is a move, not an introduction.
- Class cards are never candidates: a class "changes" whenever one of its methods does.
- A (symbol, month) pair is dropped when two or more qualifying commits changed it in that month.
- The month is the commit's `committed_at`, in UTC.

### 4. Gold is one relevance unit per evidence key

Required evidence is the commit sha and, when the subject references one, the PR number.
Supporting evidence is the symbol anchor, kept for checkpoint C. **A relevance unit is one
required evidence key; recall@k is the fraction of units with at least one resolved document in
the top k.** Because of §2, every document of a change satisfies every key of that change, so a
generated record scores 0 or 1: did any document of the right change reach the top k? The
exception is a PR whose documents include another commit's message — a PR that landed in more
than one commit: that message satisfies the PR's unit but not the commit's, and the record scores
0.5. Slice B2 counts how often this happens. The runner records `n_units` and
`max_parts_per_unit` beside it.

### 5. Leakage is measured at generation, and filtered before anything is scored

Each generated record carries, in the schema's existing `LeakageProbe` fields:
`gold_symbol_df` (pool documents containing `X`'s short name), `baseline_grep_r10` and
`baseline_bm25_r10`, measured against the pool at generation time. A record is retained only if:

- grep does not rank a gold document at rank 1 (ADR-0002 step 4), and
- the question shares no identifier-aware token of pool document frequency ≤ 5 with a gold
  document (ADR-0002 step 5).

Every candidate is generated and filtered — nothing is sampled — so the retention rate is a
census figure, and every retained record is kept.

### 6. The gate, fixed here before any record exists

Checkpoint B's leakage verdict, computed on the **dev** split only:

| Condition | Value | Why |
|---|---|---|
| Retained dev records | **≥ 100** | Fewer is INCONCLUSIVE, never PASS |
| Strongest *lexical* baseline (grep, BM25-default, BM25-ident) | Wilson 95% upper bound on recall@10 **< 0.35** | A point estimate can pass by luck (findings 8, 14). At ADR-0002's 0.175 and n = 100 the upper bound is ≈ 0.26 |
| Retention rate | **≥ 25%**, reported filtered and unfiltered | Below it, the filter has selected an unrepresentative subset — selection on the dependent variable |

Dense is reported beside them but does not enter the verdict: a strong learned retriever is a
result, not a leak.

### 7. Split

`split: Literal["dev", "test"]` is a required field, assigned at generation. Records are grouped
by PR number (by commit sha when there is no PR), and a whole group goes to dev when
`sha256(seed : group)`, read as a number in [0, 1), falls below 0.6 — so one PR's records never
sit on both sides (finding 2), and the assignment is independent of run and record order. It is
per group rather than per query class, because one PR can yield both an "added" and a "changed"
question; since the hash ignores the class, each class lands near 60% dev, and the generator
reports each class's actual share, expected within 55–65%. Every gate and every tuning decision
reads dev. The
runner refuses `--update-baseline` on test, and test is opened at most twice in the project, each
time in a dated commit.

### 8. Sizes follow from the minimum detectable effect

For paired 0/1 outcomes, a net improvement is significant at 95% when it exceeds
1.96·√(discordant queries) (finding 7), so the minimum detectable effect is
**MDE ≈ 1.96·√(f / n)**, where f is the share of queries on which two systems disagree. At
f = 0.10: 4.4 points at n = 200, **3.3 points at n = 360**, 4.0 points at n = 240, and 2 points
needs n ≈ 960. Minimums: **≥ 360 retained dev and ≥ 240 retained test.** Detecting a 2-point
difference is out of budget, and the write-up says so. Supply is not the constraint: the
in-scope history holds 11,680 modified (commit, file) revisions on 5,419 non-mechanical,
PR-referencing commits, before "added" candidates are counted.

## Consequences

- **The text baselines are expected to score low**, because most function names never appear in
  their own rationale. That low score is the measured motivation for checkpoint C, whose graph
  walk (symbol → commit → PR) is evaluated against the same pool, the same units and the same
  split.
- **Slice B can finish without GitHub.** If the backfill slips, the benchmark runs on commit
  messages alone — the fallback ROADMAP §6 already names.
- **Code chunks leave checkpoint B.** They are what checkpoint C's symbol identity and graph walk
  need, so they move to C-prep.
- **The three hand-written CI seeds are retired.** They use pre-2025 `airflow/...` paths and
  code-only anchors, so they can never resolve against a rationale pool. CI's retrieval gate
  moves to a committed subset of generated records in slice B5; until then it runs on an empty
  set and says so — the pre-existing week-1 state, now with an owner and a date.

### Known limits, stated rather than hidden

- AST-only anchors cannot see a symbol moved between files in different commits, nor one
  renamed in place; checkpoint C's identity work exists for exactly these.
- The walk is first-parent, so commits reachable only through a merge are attributed to the
  merge (2,107 of 40,995).
- JIRA-era issues are outside the pool; PR text is the oldest rationale reachable for 2016–2019.
- A 2-point difference cannot be detected at these sizes.

## Alternatives considered

- **Search code, then follow links to rationale.** Rejected. grep plus one hop answers most
  questions, and the questions that survive the filter are exactly those blame cannot reach,
  which inflates checkpoint C's lift by construction.
- **A separate `documents` table.** Cleaner in principle; rejected because the resolver, the
  probe, the BM25 index and the embedding tables would all need re-pointing for no measured
  benefit.
- **LLM-paraphrased questions.** Rejected, as in ADR-0002: model-dependent and not reproducible.
- **Sampling to a fixed size.** Rejected: generating and filtering every candidate makes the
  retention rate a census figure and only adds power.
- **Unanswerable questions now.** Deferred to checkpoint D. They test refusal, which does not
  exist until D, and `UnanswerableStrategy` already supports them.

## Errata

- **2026-10-28 — the PR count in Context, and what the pool links (slice B2).** Context §3's
  "6,440 distinct PR numbers" cannot be reproduced by any rule tried. Measured under B2's rule
  ([tasks/eval-harness/002](tasks/eval-harness/002_commit_message_pool.md),
  [results/pool_census.json](../results/pool_census.json)): of the 6,810 pool commits, 5,963
  link to 5,963 distinct apache/airflow PRs and 708 carry no reference. 138 `Merge pull request
  #N` subjects from before commit `1f63537c9b` (2015-06-07) number PRs in an earlier repository,
  and one 2018 subject names a PR in a contributor's fork; none of the 139 is linked. No PR has
  two documents, so §4's 0.5 case does not arise in the commit-message pool. Commit messages
  are chunks, so they will embed through `embeddings_code_*`; migration 0004's
  `commit_message:` prose prefix stays unused. The decisions above are unchanged.

## Addenda

- **2026-10-29 — two more ambiguity rules for §3, committed before any generated record.** A
  design-time census ([tasks/eval-harness/003](tasks/eval-harness/003_golden_set_generator.md))
  found two kinds of question §3's rules let through, and both have two possible answers:
  - **Added more than once.** A symbol deleted and later re-added in the same file has two
    "added" commits. An "added" candidate is kept only if its symbol was added exactly once in
    that file's rename-aware history (115 of 2,493 candidates).
  - **One name, several files.** The "changed" template does not name the file, so a qualified
    name that exists in two or more files at HEAD makes the question ambiguous. A "changed"
    candidate is kept only if its qualified name exists in exactly one file at HEAD (27 names,
    187 of 6,059 candidates).

  Both follow §3's own rule that every question be answerable and unambiguous. Neither touches
  §5's leakage filters, §6's gate or §8's sizes, and the census measured candidate supply only:
  the baselines' recall, which is the gate, was not measured before this addendum.
- **2026-10-29 — what PR text joins §1's pool, committed before the first fetch.** Slice B4
  ([tasks/eval-harness/004](tasks/eval-harness/004_github_backfill.md)) fetches the 5,963 PRs
  that B2 linked. Three rules extend §1–§2:
  - **Conversation comments are documents.** A PR's conversation comments — GitHub serves them as
    issue comments — become `issue_comment` documents at `pull/<n>/comment/<id>`, beside the
    description (`pull/<n>`) and the inline review comments (`pull/<n>/review/<id>`). Much of a
    PR's *why* is argued there.
  - **Bot-written text is never a document.** A description or comment whose author GitHub
    marks as a bot account is stored but not pooled: a welcome or stale-bot message would
    otherwise count as gold for every PR it appears on.
  - **A description's HTML comments are removed.** They hold the PR template's instructions,
    which GitHub never displays.

  Every document is evidence for its own artifact, for its PR and for the commit B2 linked to
  that PR, as §2 requires. Issues stay outside B's pool. None of this touches §5's filters, §6's
  gate or §8's sizes: the golden set is regenerated against the grown pool, and the verdict is
  re-run and reported whatever it says.

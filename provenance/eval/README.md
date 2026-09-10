# PACE evaluation harness

Built in week 1, before there is anything to evaluate. That ordering is the point:
the gate exists before the system, so no design decision gets measured against a
threshold invented after the numbers came in.

```
provenance/eval/
  schema.py                  golden-set record (pydantic); anchors, never chunk_ids
  anchors.py                 resolve_anchors(): stable anchors -> current chunk_ids
  metrics.py                 recall/mrr/nDCG/citation/refusal + stratified bootstrap
  runner.py                  the CLI gate: score, write, recompute, compare, exit code
  golden/ci_subset.jsonl     3 hand-written week-1 seeds (not a benchmark)
  baselines/retrieval.json   committed baseline the gate compares against
eval/                        (repo root, git-ignored except fixtures)
  fixtures/qvecs_<model>.npz frozen query vectors -- this is what makes CI free
  runs/<run_id>/             per_query.parquet + run.json, one dir per run
```

`PACE_EVAL_ROOT` overrides the repo-root `eval/` location (CI mounts elsewhere);
it replaces the leading `eval/` segment of `FIXTURE_PATH_TEMPLATE`, so there is
still only one fixture path convention.

Two things this package deliberately does **not** own:

* **physical schema names.** Every table and column comes from
  `provenance.graph.tables` (`CHUNKS`, `CHUNK_EVIDENCE`, `COL_REPO_ID`,
  `COL_TOMBSTONE`, …). The eval layer was written against a table `chunk` with a
  column `tombstoned_at`; neither exists, and the only way to find out was to run
  the query.
* **the frozen-embedding cache.** `runner.py` imports `FrozenEmbedder`,
  `FrozenEmbeddingMiss`, `embedding_key` and `FIXTURE_PATH_TEMPLATE` from
  `provenance.retrieve.embedder` and keeps no parallel implementation. See below.

`repo_id` is a **bigint**, resolved once per run with
`provenance.graph.repos.resolve_repo_id(conn, owner, name)`. The string
`"apache/airflow"` never reaches a query: `repositories.repo_id` is the identity
the schema uses, and the slug against a bigint column is `invalid input syntax for
type bigint` — raised only after the fixture load and the scoring loop have run.

---

## The two-tier gate

**Tier 1 — retrieval (every push, every PR).**
Zero LLM calls. Zero embedding-API calls. Zero dollars. Query vectors are read
from a committed `.npz`; the only work is one Postgres round trip per record plus
numpy. Runs in well under two minutes on 2 cores. This is the gate that must never
be skipped, because it is the one that is cheap enough to never be skipped.

**Tier 2 — end-to-end (nightly, and on `main`).**
Agent behaviour, citation correctness and refusal quality, replayed from recorded
HTTP cassettes. Cassette replay, not live calls: a live e2e gate costs money per
push, is flaky on provider hiccups, and makes a red build ambiguous ("did the
model change or did I?"). Re-recording a cassette is a deliberate, reviewed commit.

The split matters because the two halves fail for different reasons. Tier 1 fails
when retrieval got worse. Tier 2 fails when the agent got worse. Merging them into
one job means every failure needs a human to work out which.

## The frozen-fixture path — why the retrieval gate costs $0.00

`eval/fixtures/qvecs_<model_id>.npz` maps

```
key = sha256(model_id + "\x00" + BGE_QUERY_PREFIX + question).hexdigest()  ->  float32 vector
```

The model id is inside the key, not just the filename, so a file renamed by hand
cannot serve vectors from the wrong model. The hashed text is the **final** text
the model would receive — i.e. *after* the bge instruction prefix — because a
cache keyed on anything else is not a stand-in for the embedder it replaces.

**There is exactly one implementation of this path, in
`provenance/retrieve/embedder.py`.** The runner imports it. It used to carry its
own copy: same hash formula, but over the raw question, at a different path. Two
caches that agree on neither the key nor the file cannot serve each other, so
whichever one CI reached for was reliably missing — and "the fixture is missing"
is precisely the failure that tempts somebody into adding a live fallback. One
implementation is what makes the $0.00 property enforceable instead of merely
stated.

**On a miss the runner raises `FrozenEmbeddingMiss` and exits 2. It never falls
back to a live embedding call, and there is no flag that makes it.** `--embeddings`
accepts `frozen` or `none` and rejects everything else with *"there is no live
mode"*. Every question is pre-flighted before the first one is scored: a miss
found halfway through would otherwise leave a half-written per-query table whose
aggregates look like a real, catastrophic result.

(`FrozenEmbeddingMiss` inherits `RuntimeError`, not `KeyError`, so a future broad
`except KeyError` around a dict lookup cannot silently eat the one exception that
keeps CI free.)

Baking vectors is an offline, deliberate act. `pace eval fixtures` only *reports*
which questions are missing, with the exact keys a baker must produce; it never
embeds.

Consequence worth stating in the write-up: the retrieval gate is reproducible
forever. The `.npz` is committed, so the exact vectors that produced a published
number are in git, independent of whether the embedding model is still available.

## Why a 40-query recall gate is statistically incoherent

A recall@10 mean over `n` queries where each query contributes a value from a small
grid moves in steps. At **n = 40 the mean quantises to 2.5 points** (1/40): the
smallest possible change in the headline number is 0.025. So a threshold of
"fail on a regression worse than 2 points" fires **whenever any single query flips
from hit to miss** — which happens on a tie-break reorder, a re-embedding, a
Postgres plan change, or nothing at all.

That gate has two failure modes and no success mode:

* it is not a regression detector, it is a coin flip on one query; and
* the 95% bootstrap interval on recall@10 at n=40 is roughly ±0.15 wide, so the
  gate's threshold is an order of magnitude smaller than the noise it sits in.

**Fix: run the retrieval gate on the full auto-generated set (hundreds of queries),
not on a 40-query subset.** It is free — no LLM, no embedding API — so there is no
reason to subsample. The 40-ish hand-curated set is for the *e2e* tier, where each
query costs a cassette.

The runner encodes this: below `MIN_QUERIES_FOR_PAIRED_GATE = 100` shared queries
it prints the quantisation size and treats the gate as underpowered; at or above
it, a regression must clear the threshold **and** have a paired-bootstrap interval
that excludes zero.

## Statistics

* **Stratified** percentile bootstrap, `B = 10000`, seed `BOOTSTRAP_SEED = 20250131`
  committed in `metrics.py`. Resampling happens *within* `query_class`, so a
  resample can never contain zero unanswerables — otherwise refusal recall becomes
  0/0 on some resamples and its interval is quietly wrong.
* **Paired** bootstrap for baseline-vs-system: one resampled index vector applied
  to both arms. Query difficulty is common noise and cancels out of the difference,
  giving an interval typically 3–5× tighter than the classic error of eyeballing
  whether two marginal CIs overlap. Overlapping marginal CIs do **not** imply no
  difference; report `paired delta +0.04 [+0.01, +0.07], n=312` instead.
* Every number is rendered by `format_metric()` as `0.81 [0.74, 0.87], n=150`, so a
  point estimate never appears without its interval and its n.
* Refusal precision and refusal recall are always reported **together**. Precision
  alone is gameable to 1.00 by refusing exactly one obvious unanswerable; recall
  alone is gameable to 1.00 by refusing everything. Either number quoted on its own
  is worthless.
* Undefined metrics return `NaN` and are dropped with their stratum label — never
  coerced to 0.0. A query that cannot be scored is not a query that scored zero.

## Gold evidence is anchored, never chunked

Records key evidence on `commit_sha` / `pr_number` / `issue_number` /
`review_comment_id` / `(path, qualified_name)`. **Never `chunk_id`** — `GoldEvidence`
sets `extra="forbid"`, so an attempt to add one fails at load time.

`chunk_id` is a content hash. Re-splitting a function changes it, even though the
evidence a human labelled is the same code with the same history. If gold rows
pointed at chunk ids, every chunker experiment would zero out recall for reasons
that have nothing to do with retrieval quality. `resolve_anchors()` does the join
at scoring time instead, including tombstoned chunks for archaeology queries —
those questions are *about* code that no longer exists in HEAD.

It resolves through two different routes:

* **symbol anchors** `(path, qualified_name)` join `chunks` directly, with the
  tombstone predicate in the `ON` clause so an anchor that matches nothing comes
  back as a NULL row (unresolved) rather than as no row at all (not asked for).
* **commit / PR / issue / review-comment keys** go through **`chunk_evidence`**
  (migration `0005`), the derived edge table the ingester rebuilds on every
  re-chunk. `_resolve_evidence()` is the single read of it on the scoring path,
  and therefore *the* mechanism that lets the golden set survive a re-chunk.

Two details in that join that are easy to regress:

* `chunk_evidence.evidence_kind` is the `artifact_kind` **enum**, and the golden
  set's `"pr"` is the enum's `"pull_request"`. The kinds are translated and the
  parameter is cast to `artifact_kind[]`, so an unknown kind is a loud enum error.
  Casting the *column* to text instead would still work and would silently stop
  using `chunk_evidence_lookup_idx`.
* `evidence_key` is `text` for every kind — a 40-hex sha, a PR number, an issue
  number, a review-comment id — so both sides are compared as text and numbers
  never arrive as ints. Full shas and all non-commit kinds use plain equality;
  only an abbreviated sha (curation reads them off `git log`) falls back to a
  prefix match.

An anchor that resolves to nothing is reported separately and hard-fails the run
(exit 2). It is a scoring or ingest bug and must never be silently counted as a
retrieval miss. The same reasoning applies one level up: a SQL error during anchor
resolution also exits 2, because scoring on regardless would give every answerable
query 0.0 and read as a total retrieval failure. Only two states report clean
zeros and exit 0 — the corpus is not in `repositories` yet, or `chunks` is empty.

## Leakage probe

`LeakageProbe` (`baseline_grep_r10`, `baseline_bm25_r10`, `gold_symbol_df`) is
persisted **at generation time**, and generated records that lack it fail
validation. A naively generated golden set scores grep recall@10 = 0.986 — the
question quotes the answer's identifiers, so plain substring search wins and the
benchmark measures nothing. Retained records must sit at or below
`RETAINED_GREP_R10_MAX = 0.35`. Storing the probe next to the record makes the
retention decision auditable from the committed JSONL, months later.

## Baselines

`baselines/retrieval.json` starts as:

```json
{"n_queries": 0, "recall@10": 0.0, "per_query": {}, "index_version": null, "model": null}
```

`model` is a **first-class field**, checked exactly the way `index_version` already
is: a mismatch between the stored baseline and the current run is a hard error
(exit 2), not a warning. Swapping the embedding model changes what the numbers
mean; a comparison across models is not a comparison. Without this field the
easiest possible fake improvement — change the model, keep the baseline — passes
CI silently. Re-baseline deliberately with `--update-baseline`, or acknowledge the
change with `--accept-env-change`.

`per_query` is stored so the compare step can pair query-for-query. **Aggregates
are never trusted from storage**: the runner writes `per_query.parquet`, reads it
back off disk, and recomputes every headline number from what it read. A mean with
no per-query table behind it cannot be audited and does not belong in a
dissertation.

## Non-metric invariants

These catch more real bugs than the metrics do, and they are nearly free:

| check | reads | fails when |
| --- | --- | --- |
| `second_ingest_zero_net_writes` | `ingest_runs` (status `succeeded`) | a re-ingest of an unchanged corpus writes rows — identity is unstable, usually something non-deterministic leaked into `chunk_id` |
| `chunk_id_set_hash` | `chunks` | the sha256 over the sorted chunk-id set changed without a chunker version bump |
| `dead_letter_empty` | `dead_letters` | ingest "succeeded" while silently dropping documents |
| `p95_retrieval_latency` | this run's own timings | p95 exceeds 400 ms |
| `chunk_count_stable` | `chunks` | total chunk count drifted more than ±1% |

**A check that cannot fail is not a check.** Two properties enforce that, and both
were absent:

1. **A missing table may `skip`; a SQL error must `fail`.** The classifier reads
   the exception's `sqlstate` and tolerates only `42P01` / `42704` / `3F000` —
   "this part of the schema does not exist yet", which is a real week-1 state.
   Everything else (undefined column, invalid enum input, bad cast, syntax error)
   is a bug in `runner.py` and goes red. Under the old blanket
   `except Exception -> skip`, three identifiers that match no migration
   (`ingest_run`, `dead_letter`, `status = 'ok'` against an enum whose members are
   `pending|running|succeeded|failed|cancelled`) reported `skip` on every run since
   the file was written, and the idempotency and dead-letter gates could never go
   red.
2. **Each check runs in its own transaction.** They used to share one, so the
   first failing statement aborted it and every later check died with
   `InFailedSqlTransaction` — reported, of course, as `skip`. One typo silenced
   the whole suite.

`--strict-invariants` promotes the remaining skips to failures once the schema is
populated.

## Commands

`provenance/cli.py` is the single authority on the CLI surface; this Typer app is
mounted there with `typer.add_typer(runner.app, name="eval")`, and
`runner.main(argv=None)` exists for direct use (`python -m provenance.eval.runner`).
There is no `sys.argv`-rereading shim: with one, `pace eval <anything>` fed Click
an argument list that still began with `eval` and every sub-command failed with
*No such command 'eval'*.

```bash
pace eval run                          # gate with the built-in null retriever
pace eval run --retriever provenance.retrieve.router:HybridRetriever \
              --golden provenance/eval/golden/auto_full.jsonl
pace eval run --no-db --no-invariants  # week-1 smoke: no Postgres needed
pace eval run --update-baseline        # deliberate re-baseline
pace eval fixtures                     # audit the frozen query-vector cache
pace eval invariants --strict          # structural checks on their own
```

Those three sub-commands — `run`, `fixtures`, `invariants` — are the whole
surface. Anything in CI, the Makefile or the runbook that invokes
`pace eval retrieval`, `pace eval end-to-end` or `pace eval summarise` is naming
a command that does not exist.

Exit codes: `0` pass · `1` regression or failed invariant · `2` hard error
(fixture miss, model/index_version change, unresolved gold anchor).

## Week-1 status

Empty golden set, empty index, null retriever — the gate reports clean zeros and
exits 0, loudly. `golden/ci_subset.jsonl` holds three hand-written seeds with real
Airflow symbols; their commit/PR anchors are intentionally empty until week-2
curation reads real values off `git log`. Inventing plausible shas would put
fiction in the gold set; an empty field at least fails loudly when resolved.

One consequence worth stating plainly: because those three seeds carry only symbol
anchors, **CI does not yet exercise `chunk_evidence` at all**. The path that makes
gold survive a re-chunk is implemented and read only by `_resolve_evidence()`, and
the first real commit/PR anchor added during week-2 curation is also the first
test of it. Until then, "the anchors resolve" means the symbol half resolves.

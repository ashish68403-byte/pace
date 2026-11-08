# Task 007: Fix BM25's identifier queries, embed the pool, and add the dense and fused rows

**Tracker:** [006_EVAL_HARNESS](../../tracker/006_EVAL_HARNESS.md) · **checkpoint B**, slice B5b
(the second half of B5)
**Priority:** P0 · **Status:** ✅ Done (2026-11-08)
**Decision records:** [ADR-0003](../../ADR-0003-rationale-pool-benchmark.md) §1 and its 2026-10-28
erratum (pool documents embed as chunks), §6 (dense is reported beside the verdict, never in it)
and §7 (every tuning decision reads dev; test is opened at most twice);
[ADR-0004](../../ADR-0004-gold-validity-audit.md) §8 (every lift is quoted twice)
**Depends on:** [006](006_lexical_baselines.md) (the ablation and its lexical rows)

## User Story

- **Summary:** Fix how the production BM25 index reads an identifier in a question. Embed the
  69,769 pool documents and every golden question with bge-small-en-v1.5. Add three rows to the
  ablation table: the fixed BM25, dense retrieval, and their reciprocal rank fusion. Keep the old
  BM25 as a fourth, so the fix is a paired before and after. This closes B5.

### Use Case

- **As the** author defending checkpoint B
- **I want** the production retrieval stack (BM25, dense, and their fusion) scored on the same
  records as the lexical baselines, with the fusion's settings fixed before any dense number
  exists
- **so that** "does dense retrieval find rationale the words miss?" and "what does fusion add?"
  have committed, reproducible answers, and the BM25 defect that B5a measured is shown fixed.

### Background (measured 2026-11-08)

- **B5a's table** (`results/ablation.json`), on the 4,206 retained dev records: recall@10 is
  0.102 for grep, 0.140 for `bm25_ident` (the reference) and 0.044 for the production `bm25`
  (183 hits). On the df = 0 stratum (705 records) it is 0.052, 0.121 and 0.026.
- **Why the production BM25 is low.** pg_search's query parser turns an identifier that
  `pdb.source_code` splits into a phrase:
  - `_schedule_dag_run` matches the same 22 documents as the phrase `"schedule dag run"`; the
    three words, loose, match 12,093;
  - `TaskInstance.refresh_from_db` matches the same 2 documents as its five-word phrase; the
    words, loose, match 17,327;
  - the joined form `scheduledagrun`, which the ingester writes into `lexical_blob` (19
    documents), is never produced by a question, although `retrieve/lexical.py`'s docstring says
    it is.
- **A read-only check on a seeded 600-record dev sample** (kept in `~/pace-plans/runs/`, scored
  through the runner; the shipped BM25 reproduced B5a's per-query file on all 600): hits@10 are 24
  for the shipped BM25, 30 with the joined forms appended, and 91 with each identifier rewritten
  as its loose parts plus its joined form. `bm25_ident` has 95 and grep 72.
- **Dense and fusion code exists, unused.**
  - `dense.exact_search` is brute force over the table behind the `code_current` alias, in the
    index's halfvec arithmetic, with ties broken by `chunk_id`. It has no `kinds` filter.
  - `fusion.reciprocal_rank_fusion` fuses named rank lists; its default k is 60.
  - Nothing writes document vectors and nothing bakes the question vectors: `eval/fixtures/` does
    not exist.
- **The vectors' home.** The four embedding tables are empty, and `code_current` names
  `embeddings_code_blue`. By ADR-0003's erratum, pool documents are chunks and embed through
  `embeddings_code_*`; the `doc_ref`-keyed prose tables stay unused.
- **The embedder.** `LocalEmbedder` runs fastembed with bge-small-en-v1.5: 384 dimensions, two
  threads, batches of 16, and no persistent model cache. fastembed 0.8.0 is in `uv.lock` as the
  `ml` extra, not installed. `query_text()` adds bge's query prefix. `FrozenEmbedder` reads a flat
  `.npz` keyed by `embedding_key(model, final text)`, and the runner already hands every retriever
  its question's frozen vector.
- **The pool's lengths.** 69,769 documents, 24.3 million characters. Over 2,000 characters, about
  bge's 512-token window: 1.7% of commit messages, 14.7% of PR descriptions, 4.7% of conversation
  comments and 0.4% of review comments, about 2.9% of the pool.
- **The machine.** 4 logical CPUs and 4.9 GB of RAM, about 3 GB of it free. B5a's ablation peaked
  at 1.36 GB.
- **Decisions made with the user, 2026-11-08:**
  - one slice, with the BM25 fix first;
  - fusion uses k = 60, a depth of 100 and equal weights, fixed by this spec before any dense
    number exists. A sweep over k is reported as sensitivity only, never used to choose;
  - approach A, the production path: vectors in the database, exact search, frozen question
    vectors. Not an in-memory matrix, and not frozen document vectors;
  - the old phrase behaviour stays reachable, as the row `bm25_phrase`.

## Design

### Units

| Unit | Kind | Responsibility |
|---|---|---|
| `provenance/retrieve/lexical.py` | DB | `identifiers="loose"` or `"phrase"` on `lexical_search`; the rewrite; `Bm25Retriever` takes the mode; the corrected docstring. |
| `provenance/retrieve/dense.py` | DB | A `kinds` filter on `exact_search`; `DenseRetriever`. |
| `provenance/retrieve/fusion.py` | pure, over two retrievers | `RrfRetriever`, and the fixed `FUSION_DEPTH`. |
| `provenance/retrieve/embedder.py`, `provenance/config.py` | — | A persistent model cache folder. |
| `provenance/ingest/pool_embedding.py` | DB | `pace pool embed`: choose, embed, write, fingerprint, record. |
| `provenance/eval/query_vectors.py` | pure, then a file | `pace eval bake`: every golden question's frozen vector. |
| `provenance/eval/ablation.py` | DB, then pure | Version 2: six rows, the named pairs, the k sensitivity, the new refusals. |

### BM25's identifier queries

- **`lexical_search(..., identifiers="loose")`** is the new default. After the query is
  sanitised, every identifier that splits into more than one part is replaced by its parts and its
  joined form, and dots become spaces: `TaskInstance.refresh_from_db` is searched as
  `task instance taskinstance refresh from db refreshfromdb`. The split is
  `chunker.split_identifier`, the function that builds `lexical_blob` and the probe's tokens, so
  the query and the index cannot drift apart.
- **`identifiers="phrase"`** is today's behaviour. It is kept only so that the ablation can score
  the old index beside the new one, on the same records.
- **The qualified-name field still gets the exact sanitised query.** A one-word query like
  `_normalise_url` must stay one token there. Loosening would add spaces, and that lookup runs
  only on a query without them.
- **`Bm25Retriever(repo_id, identifiers=...)`** gives two rows: `bm25` (loose) and `bm25_phrase`.
- **The module docstring** states the measured facts above, not the claim that a typed identifier
  produces the joined form.
- **The rewrite was chosen on a 600-record dev sample** and is reported on all of dev. ADR-0003 §7
  allows that, because every tuning decision reads dev; the test split, when it opens, is the
  unbiased check.

### Embedding the pool: `pace pool embed`

- **Setup:** `uv sync --extra ml`, once. The model downloads on first use into a persistent
  folder, `settings.embedding_cache_dir` (default `~/.cache/pace/fastembed`), not a temporary one.
- **What it embeds:** every pool document (the four rationale kinds, not tombstoned) that has no
  vector for `settings.embedding_model_version`. Chunk ids are content hashes, so changed text
  arrives as a new chunk with no vector; a stored vector whose `content_sha` disagrees with its
  chunk's counts as missing all the same. Vectors go into the table `code_current` names, with the
  chunk's `content_sha` and visibility `public`.
- **The text** is the document as stored, with no prefix: bge's prefix is for queries only. The
  model truncates at 512 tokens. The command counts the truncated documents, by kind, with the
  model's own tokenizer.
- **Order:** by length, then `chunk_id`. Each batch then holds similar lengths, which wastes less
  padding, and a document always lands in the same batch.
- **Resumable:** each group of documents is written in its own transaction, so an interrupted run
  continues where it stopped.
- **`--dry-run N`** embeds a seeded random sample of N documents, writes nothing and prints the
  rate, so the full run's length is measured before it starts.
- **`results/pool_embedding.json`** is committed and holds no text:
  - the model, the SHA-256 of the model file and the fastembed version;
  - documents and truncations per kind, and the wall time;
  - the **fingerprint**: one SHA-256 over every pool document's `chunk_id` and stored vector, in
    `chunk_id` order.

### Freezing the questions: `pace eval bake`

- **Every question in `provenance/eval/golden/auto_full.jsonl`**, 8,250 of them, dev and test, is
  embedded as `query_text(question)`, with bge's prefix exactly once. The vectors go into
  `eval/fixtures/qvecs_bge-small-en-v1.5.npz`, keyed by `embedding_key`: the file and format
  `FrozenEmbedder` already reads.
- **Test questions are included.** Embedding a question does not score it, so the test split stays
  sealed, and the file needs no rebuild when test opens.
- **About 13 MB, committed.** It is computed only from committed questions.
- **`pace eval fixtures --golden provenance/eval/golden/auto_full.jsonl`** then reports no question
  missing.
- **Before baking,** the plan checks once that fastembed's `query_embed` does not add the prefix a
  second time.

### Document vectors stay out of the repository

They are computed from pool text, so DATA-PROTECTION §3 covers them: they live only in `pace-db`
and rebuild with `pace pool embed`, and the repository commits their fingerprint. §3 gains one
paragraph saying so, in this spec's commit.

### The dense and fused rows

- **`DenseRetriever` (`dense`)** ranks with `exact_search` over the question's frozen vector, with
  `kinds=RATIONALE_KINDS`. It refuses to run without a vector. Exact search orders by distance,
  then `chunk_id`, so it is deterministic and its top 10 is the head of its top 100.
- **`RrfRetriever` (`rrf`)** fuses the loose BM25's top 100 and dense's top 100 with
  `reciprocal_rank_fusion`, then keeps the top 10.
  - **Fixed here, before any dense number exists:** k = 60, the original paper's value; a depth of
    100 per list; equal weights.
  - Dense's top 100 is computed once per question and shared with the `dense` row.
  - The `bm25` row keeps its own top-10 call. `lexical_search` merges its fields after an
    over-fetch, so a deeper fetch can reorder the top 10, and the row must measure exactly what
    `Bm25Retriever` returns.

### The ablation, version 2

`pace eval ablation` keeps its options and gains `--fixture` and `--embedding-record`.

- **Six rows:** `grep`, `bm25_ident` (the reference), `bm25_phrase`, `bm25`, `dense` and `rrf`.
  Each has what B5a's rows have: hits@10, and recall@10, MRR@10 and nDCG@10 with intervals, per
  query class and per df stratum.
- **Comparisons:** every row against `bm25_ident`, as in B5a, and three named pairs:
  - **the fix:** `bm25` against `bm25_phrase`;
  - **what dense adds to BM25:** `rrf` against `bm25`;
  - **what BM25 adds to dense:** `rrf` against `dense`.

  Each has its paired interval, D, the MDE, and the difference divided by the gold's validity
  (ADR-0004 §8).
- **The k sensitivity:** `rrf`'s recall@10 at k = 10, 20, 30, 60, 100 and 200, from the same
  top-100 lists and through the runner's own scoring, labelled "sensitivity only; k = 60 was fixed
  in the spec".
- **Meta** gains the model, the vectors' fingerprint and the SHA-256 of the question file.
- **The per-query file** carries each record's first gold rank for all six rows.
- **It refuses, and writes nothing, unless:**
  1. `grep` and `bm25_ident` reproduce the verdict's hits, 431 and 588, as in B5a;
  2. every pool document has a vector for the model, carrying its chunk's `content_sha`;
  3. the vectors' fingerprint equals the one in `results/pool_embedding.json`;
  4. every scored question has a frozen vector.
- **Byte for byte:** two runs over the same stored vectors write the same bytes. Embedding again on
  another machine may change the last bits of some floats; the fingerprint is how a reader tells.
- **The leakage probe and its verdict are untouched.** Dense never enters the verdict (ADR-0003
  §6).
- **Cost:** B5a's 23 minutes plus three rows of database queries. The plan times dense's exact
  scans on a sample first.

### Not in this slice

- **Tuning the vector index** (`ef_search`, through `measured_recall`) and serving latency:
  checkpoint D, when a user's query uses the index.
- **Splitting long documents into windows:** the ~3% truncated at 512 tokens are counted, not
  fixed.
- **The leakage probe's dense row:** it reads document vectors from the frozen file, which this
  slice does not commit.
- **The test split:** still sealed.
- **Tuning fusion's weights or k, or BM25's field boosts.**
- **Blue/green rebuild tooling:** this is the first build, into the empty live table.
- **A bigger or hosted embedding model.**

## Acceptance Criteria

- **Scenario:** an identifier in a question is searched by its parts
  - **Then:**
    - `TaskInstance.refresh_from_db`, `_schedule_dag_run` and `camelCaseName` become their parts
      plus their joined forms; `alpha`, `2020` and plain English are unchanged;
    - the qualified-name field still receives a one-word query unchanged;
    - on `history_repo`, `phrase` keeps today's ranking, and `loose` finds a document that names
      only some of an identifier's parts.

- **Scenario:** dense searches the pool, and only the pool
  - **Given:** `history_repo`'s pool embedded by a fake embedder, plus a planted chunk of another
    kind whose vector is closer to the query than any pool document's
  - **Then:** `dense` never returns the planted chunk, and `dense` without a vector refuses.

- **Scenario:** fusion follows this spec's settings
  - **Then:** `rrf` fuses top-100 lists with k = 60 and equal weights; a document in both lists
    outranks one at the same rank in a single list; ties break on `chunk_id`.

- **Scenario:** embedding the pool
  - **Given:** `history_repo`'s pool and a fake embedder
  - **Then:**
    - one vector per document, each with its chunk's `content_sha`;
    - a run stopped after one group, then run again, writes no duplicates and finishes the rest;
    - a new document is embedded on the next run, and a vector whose `content_sha` disagrees with
      its chunk's is written again;
    - the fingerprint changes when any vector changes, and the record names the model.

- **Scenario:** baking the questions
  - **Given:** a fake embedder
  - **Then:** the file holds one float32 vector of 384 per question, keyed by
    `embedding_key(model, query_text(question))`, and `pace eval fixtures` reports none missing.

- **Scenario:** the ablation, version 2
  - **Given:** `history_repo`, with a fake embedder's document vectors and question file
  - **Then:**
    - six rows and the three named pairs; two runs write the same bytes;
    - a missing vector, a stale `content_sha`, a fingerprint that disagrees with the record, or a
      question with no frozen vector each fails, and writes nothing.

- **Scenario:** the real run
  - **Given:** `pace`
  - **Then:**
    - `pace pool embed` covers 69,769 of 69,769 documents, and `results/pool_embedding.json` is
      committed;
    - `pace eval fixtures` reports no question missing, and the question file is committed;
    - `bm25_phrase` reproduces B5a's `bm25` first gold rank on all 4,206 records, and the loose
      `bm25` reproduces the check's 91 of 600 on its sample;
    - the four refusal checks pass, and a second run writes the same bytes.

- **Scenario:** the documents agree
  - **Then:**
    - DATA-PROTECTION §3's paragraph on vectors is committed with this spec;
    - `results/README.md` lists `pool_embedding.json` and describes version 2 of `ablation.json`;
    - the eval README lists `pace eval bake`, and the embedder's docstring gives the measured
      local time;
    - tracker 006 has a dated B5b note, and the ROADMAP says B5 is done.

## Files to Modify

- New: `provenance/ingest/pool_embedding.py`, `provenance/eval/query_vectors.py`
- `provenance/retrieve/lexical.py`, `provenance/retrieve/dense.py`, `provenance/retrieve/fusion.py`,
  `provenance/retrieve/embedder.py`, `provenance/config.py`, `provenance/eval/ablation.py`,
  `provenance/eval/runner.py` (the `bake` command), `provenance/cli.py` (`pace pool embed`)
- Results: `results/pool_embedding.json` and `eval/fixtures/qvecs_bge-small-en-v1.5.npz` (new);
  `results/ablation.json`, `results/ablation_per_query.jsonl`
- Tests: unit tests for the rewrite, fusion, the question file and the ablation's pure half;
  integration tests for embedding the pool, dense over the pool only, and the ablation end to end
- In the spec commit: `docs/DATA-PROTECTION.md` (§3, vectors), `docs/tracker/006_EVAL_HARNESS.md`
  (header)
- Later: `provenance/eval/README.md`, `results/README.md`, `docs/tracker/006_EVAL_HARNESS.md`,
  `docs/ROADMAP.md`

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    uv sync --extra ml
    uv run pace pool embed --dry-run 1000      # the rate, before the full run
    uv run pace pool embed
    uv run pace eval bake
    uv run pace eval fixtures --golden provenance/eval/golden/auto_full.jsonl
    uv run pace eval ablation                  # twice: both files byte-identical

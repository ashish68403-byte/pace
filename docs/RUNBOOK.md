# PACE runbook

Operational reference for a single developer on one machine. Three sections:
**start everything**, **what happens when a dependency fails**, **how to read a trace**.

Verified environment: Ubuntu 24.04 under WSL2, Python 3.12.3, uv 0.11.16,
Docker 29.1.3 with **Compose v2.40.3**. `docker-compose.yml` is the single description of
both containers; the Makefile and `scripts/bootstrap.sh` drive `docker compose` rather
than carrying their own `docker run` lines, so the image digests and the PG18 volume
mount are defined exactly once.
Hardware: 2 physical cores, ~3.7 GB RAM visible to WSL, no CUDA.

Every `pace …` command in this document is a real sub-command of `provenance/cli.py`:
`demo · ingest · scope · eval {run,fixtures,invariants} · leakage · health · version`.
If you find one here that is not on that list, the document is wrong and the CLI is right
— an earlier draft of this runbook, the Makefile and both workflows documented
`pace serve`, `pace corpus load` and `pace eval retrieval`, none of which ever existed.

---

## 1. Start everything

### Cold start, first time on a machine

```bash
git clone <repo> ~/pace && cd ~/pace
./scripts/bootstrap.sh
```

`bootstrap.sh` is idempotent. It checks for `uv` and the compose plugin, creates `.venv`
on Python 3.12, runs `uv sync --extra dev` (no `--frozen`: there is no committed
`uv.lock`, so `--frozen` fails outright), brings `pace-db` up through `docker compose`,
waits for readiness *by polling a real query* (see the note below), ensures `vector` and
`pg_search`, runs `alembic upgrade head`, and prints a healthcheck including the
tokenizer probe.

> **Why not `pg_isready`.** The postgres entrypoint runs `initdb` against a temporary
> server before starting the real one. `pg_isready` answers "accepting connections"
> against that temporary server, the wait loop goes green, the temporary server then
> shuts down, and migrations fail with `server closed the connection unexpectedly` —
> which looks like an Alembic bug and is not. Everything in this project waits by running
> `SELECT 1` as the real user against the real database over the real TCP listener.

### Day-to-day

```bash
make up                 # pace-db + jaeger v2, blocking on the real healthchecks
make down               # stop both, keep the pace-pgdata volume
make test-unit          # the fast half: no database, runs anywhere

# There is no `pace serve`. The API is a FastAPI app; uvicorn runs it:
uv run uvicorn provenance.api.main:app --reload --port 8000
```

| service | port | what it is |
|---|---|---|
| `pace-db` | 5432 | ParadeDB (PostgreSQL 18.6 + pgvector 0.8.4 + pg_search 0.25.7), digest-pinned |
| `pace-jaeger` | 16686 | Jaeger **v2** UI (`jaegertracing/jaeger:2.x`, *not* the dead `all-in-one` v1 line) |
| | 4317 / 4318 | OTLP gRPC / HTTP. `settings.otel_endpoint` points at 4318. |

### Verify the stack is actually healthy

```bash
uv run pace health                          # db reachable, extensions present, migration head
curl -s localhost:8000/healthz | jq         # app-level, includes trace_id
docker exec pace-db psql -U postgres -d pace -tAc \
  "SELECT '_normalise_url camelCaseName'::pdb.source_code::text[];"
  # must print {normalise,url,camel,case,name}
```

That last one is not decoration. The whole identifier does **not** survive
`pdb.source_code` tokenization — only its parts do. The obvious response, indexing the
same column twice under two casts, is **not available**: pg_search rejects it outright
(*"indexed attribute content defined more than once"*), and a table may hold only one
ParadeDB index at all. So there is exactly ONE bm25 index, over three *distinct* columns:

```sql
CREATE INDEX chunks_bm25_idx ON chunks USING bm25 (
    chunk_row_id,                     -- key_field: what paradedb.score() must be passed
    (content::pdb.source_code),       -- split tokens
    (qualified_name::pdb.literal),    -- the exact dotted symbol, as one token
    (lexical_blob::pdb.source_code)   -- the joined form the tokenizer destroys
) WITH (key_field = 'chunk_row_id');
```

`lexical_blob` carries the underscore-free joined form (`normaliseurl`); the ingester
emits it because no tokenizer will. If the tokenizer output above ever changes, lexical
recall changes with it and every retrieval number in the dissertation needs re-measuring.
`tests/integration/test_schema.py` asserts every clause of this against the live
catalogue on each CI run.

### Corpus

```bash
export GITHUB_TOKEN=...                     # read-only
uv run python scripts/fetch_corpus.py manifest --since 2023-01-01
uv run python scripts/fetch_corpus.py hydrate    # gitignored body cache
uv run python scripts/fetch_corpus.py verify
```

Only the ID manifest is committed. Bodies are the comment authors' copyright and are
fetched at runtime — see `docs/DATA-PROTECTION.md`.

### Before pushing

```bash
make lint && make typecheck && uv run pytest -q   # db tests skip if pace-db is down
uv run pace leakage --from-db                     # or --pool <committed pool>.jsonl
```

The probe defaults to the committed golden set, `provenance/eval/golden/ci_subset.jsonl`
(the only one that exists; `eval/golden/golden.jsonl` and `golden_set.jsonl` were
documented and never written). It needs a document pool and will not invent one: pass
`--from-db` to read the live index, or `--pool` to read a committed JSONL. Before the
first ingest both are empty and the verdict is **INCONCLUSIVE**, exit 2 — *not* PASS. A
run that resolves zero gold measures nothing, and printing PASS for it was the worst
defect in this tree.

The leakage probe is the invariant that makes every other number meaningful. It reports
four baselines — literal `grep`, BM25 with a default tokenizer, BM25 with the
identifier-aware tokenizer, and dense-only over frozen vectors — because *lift* has to be
quoted against the strongest cheap baseline, not the most convenient weak one. It also
prints a pasteable one-paragraph summary. If grep reads recall@10 ≥ 0.35 on the retained
set, stop and fix the golden set; do not tune the retriever against a benchmark that grep
can answer.

---

## 2. Graceful degradation

**One defined behaviour per dependency failure.** The rule the whole table enforces:
*when a dependency is unavailable, PACE degrades to a narrower answer or an explicit
refusal — it never silently substitutes a cheaper path and reports the result as if
nothing happened.* A system whose selling point is "I refuse when the rationale was never
written down" cannot afford a failure mode that looks like a confident answer.

| Dependency | How the failure is detected | Defined behaviour | What the user sees |
|---|---|---|---|
| **Postgres unreachable** | connection pool raises on checkout; `/healthz` probe fails | Fail closed. No retrieval is possible without the index; there is no in-memory fallback. Pool retries 3× over 2 s, then gives up. | `503`, body `{"error": "index_unavailable", "trace_id": ...}`. `/healthz` red. |
| **`pg_search` extension missing / BM25 index absent** | startup check queries `pg_extension` and `pg_search` index catalogue | Fail closed at startup, not at query time. Refusing to boot is better than serving dense-only results that quietly under-retrieve exact identifiers. | Process exits non-zero with the missing-extension name. |
| **`pgvector` present but no vectors loaded** (corpus ingested, not embedded) | startup resolves the `code_current` alias in `index_aliases` and counts rows in the physical table it names — there is no `embedding` column on `chunks`; vectors live in `embeddings_code_blue`/`_green` | Degrade to **lexical-only** retrieval, and say so. Hybrid weights are renormalised to lexical=1.0. | Answers include `"degraded": ["dense_retrieval_unavailable"]`; the API response and the CLI both print the banner. |
| **Embedding model unavailable** (`ml` extra not installed, or model files absent) | import guard at first use | Same as above: lexical-only, flagged. Never falls back to a remote embedding API — that turns a local failure into a bill. | `"degraded": ["dense_retrieval_unavailable"]` |
| **Frozen embedding cache MISS during eval** | `FrozenEmbeddings` lookup returns nothing | **Hard failure, always.** Never a live call. This is the single most important non-negotiable in the eval path. | Eval exits non-zero: `frozen embedding cache MISS for query …`. CI goes red. |
| **LLM provider down / rate-limited / no key** | httpx error, 429, or missing credential | Retrieval still runs. The system returns the **retrieved evidence with citations and no generated synthesis** — which is a genuinely useful degraded product, not an error page. | `200` with `"answer": null`, `"citations": [...]`, `"degraded": ["synthesis_unavailable"]` |
| **LLM returns a citation that does not resolve** | post-hoc validation against the `provenance_edges` view and `chunks` | Answer is **suppressed and converted to a refusal.** An uncheckable citation is worse than no answer. | `"refusal": "cited evidence did not resolve"`, with the offending anchors listed. |
| **Jaeger / OTLP collector down** | exporter connect error | Degrade silently *for the request*, loudly *in the log*. Spans are dropped after a bounded in-memory queue fills; request handling is never blocked on telemetry. | Request succeeds normally. `trace_id` is still returned and still valid — it just will not be findable in the UI. One WARN per minute, rate-limited. |
| **GitHub API rate-limited or down** | 403 with rate-limit header, 429, 5xx | `fetch_corpus.py` sleeps until reset for primary limits, exponential backoff for secondary/5xx, then gives up cleanly leaving a partial manifest. Never a partial *record*. | Progress on stderr; partial manifest is valid JSONL and re-runnable. |
| **Body cache miss at query time** | cached file absent for a manifest record | The artefact is excluded from the answer and named in the degradation list. Never fetched inline — a synchronous GitHub call inside a request is an unbounded latency and a rate-limit hazard. | `"degraded": ["N artefacts not hydrated"]`; run `fetch_corpus.py hydrate`. |
| **Corpus git repository missing at `~/corpus/airflow`** | `pygit2` open fails at startup | Commit-level provenance disabled; PR/issue/comment provenance still works. Flagged at startup and on every response. | `"degraded": ["commit_provenance_unavailable"]` |
| **Disk full** (3.7 GB machine; index builds are the usual cause) | Postgres `53100`, or `OSError: [Errno 28]` in the hydrate cache | Writes fail fast; no partial index is left half-built because index creation is inside a transaction. | `503`. Recover with `docker volume ls`, prune old images, re-run `alembic upgrade head`. |

Two rules that hold across every row:

1. **Every degraded response carries `degraded: [...]` and a `trace_id`.** A silent
   degradation is indistinguishable from a bug and destroys the eval — a nightly run that
   quietly went lexical-only for six hours produces a real-looking recall drop that costs
   a day to diagnose.
2. **Degradation never crosses the money line.** No failure path anywhere in the system
   turns into a live LLM or embedding API call that would not have happened anyway.

---

## 3. Reading a trace when a query is slow

Every request produces **one complete trace**, and the response body carries its
`trace_id`. That was the highest-leverage decision in the project: it turns "it felt
slow" into a URL.

### Get the trace

```bash
curl -s -X POST localhost:8000/query \
  -H 'content-type: application/json' \
  -d '{"question": "why does the scheduler re-queue a task after a heartbeat timeout?"}' \
  | jq '{trace_id, degraded, latency_ms}'
```

Paste the `trace_id` into the Jaeger UI search box at <http://localhost:16686>
(*Search → by Trace ID*). Do not go hunting by service and operation name; the id is
right there in the response.

### The span tree you should see

```
pace.query                                        (root, ~total latency)
├── pace.retrieve.lexical        pg_search BM25 over both indexed forms
├── pace.retrieve.dense          pgvector ANN
├── pace.retrieve.fuse           reciprocal rank fusion
├── pace.graph.expand            one to three hops over the typed edge tables
├── pace.evidence.hydrate        body cache reads
├── pace.synthesise              the only span that can call an LLM
└── pace.cite.validate           every citation resolved back to a live row
```

### Where the time actually goes, and what each case means

| Symptom in the trace | Almost always means | Fix |
|---|---|---|
| `pace.retrieve.lexical` dominates (> 500 ms) | The single bm25 index is not being used — usually a predicate that casts and disables it, or `paradedb.score()` was passed `chunk_id` instead of the `chunk_row_id` key field | `EXPLAIN (ANALYZE, BUFFERS)` the span's `db.statement` attribute; confirm `chunks_bm25_idx` is the plan's index |
| `pace.retrieve.dense` dominates | ANN fell back to a sequential scan — nearly always a probe not cast IDENTICALLY to the index (`embedding::halfvec(384)`, via `tables.halfvec_probe()`), which returns correct rows and silently stops using the index — or `ef_search` set too high for 2 cores | `EXPLAIN` the span's `db.statement`; confirm the plan names `embeddings_code_*_hnsw` |
| `pace.graph.expand` has **many short child spans** | N+1 traversal: expanding one edge per round trip instead of one query per edge type | Batch the hop. This is the most common performance bug in the codebase's shape |
| `pace.evidence.hydrate` is slow | Cold body cache, or cache on a Windows-mounted path (`/mnt/c`) rather than ext4 | Keep the corpus on ext4 under `~`; `/mnt/c` I/O under WSL2 is an order of magnitude slower |
| `pace.synthesise` is 80–95 % of the trace | Normal. LLM latency dominates a healthy request | Nothing to fix; optimise retrieval only if the *other* spans are large in absolute terms |
| Root span much longer than the sum of children | Time is outside instrumented code — connection-pool wait, or GIL contention on 2 cores | Look at `db.pool.wait_ms` on the root; raise pool size or lower concurrency |
| Trace ends at `pace.cite.validate` with an error | A citation did not resolve — the refusal path fired correctly | Check whether the chunker changed without the golden set being re-anchored |
| **No trace at all**, but the response has a `trace_id` | Jaeger is down or the exporter queue overflowed | `docker ps` for `pace-jaeger`; this is the one degradation that is invisible in the response by design |

### Span attributes worth knowing

- `pace.query_id`, `pace.degraded` — on the root, so a degraded request is identifiable
  from the trace list without opening it.
- `db.statement` — on every Postgres span. Copy it straight into `psql` with `EXPLAIN`.
- `pace.retrieval.k`, `pace.retrieval.n_candidates` — on the retrieve spans.
- `pace.chunk_ids` — on `cite.validate`, so a non-resolving citation names itself.
- `gen_ai.usage.input_tokens` / `output_tokens` — on `synthesise`, the only span that
  costs money. If it appears in a nightly or CI trace, something has crossed the money
  line and that is a bug.

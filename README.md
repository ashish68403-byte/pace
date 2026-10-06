# PACE - Provenance-Aware Code Intelligence

PACE answers one question over a real codebase: **"why is this code the way it is?"**

You point it at a piece of `apache/airflow` and it returns the rationale with citations -
the commit that introduced it, the PR that argued about it, the issue that motivated it,
the inline review comment where a maintainer said "no, do it this way". When the rationale
was never written down, PACE says so and **refuses** rather than inventing a plausible one.
The refusal is a first-class output, not a failure mode. A code-explanation system that
always produces an answer is indistinguishable from one that guesses.

Final-year project. Built solo, in 14 weeks.

---

## Honest scope

- **Solo, 14 weeks.** Everything below is sized to fit that, on the hardware listed further down.
- **A slice of Airflow, not all of Airflow.** The study scope is roughly **60-80k lines**, chosen
  by `pace scope` - not the ~200k-line full tree. The scope selection is committed and versioned,
  so every number in the writeup is attached to a fixed corpus.
- **One language.** Python chunking via tree-sitter. No multi-language story.
- **Postgres does the retrieval.** pgvector for dense, `pg_search` (BM25) for lexical, in one
  database. No separate vector service, no separate search cluster.
- **Phase 0 comes first.** Instrumentation, the eval harness, the golden-set schema and CI exist
  *before* the agent does. This is deliberate: a retrieval project that builds the agent first has
  no way to tell whether any later change helped.

## What this is not

- Not a code-generation or autofix tool. It explains and cites; it does not write your patch.
- Not a general-purpose chatbot over the repo. Out-of-scope questions are refused, not improvised.
- Not a claim about all repositories. Airflow has unusually good written rationale in its PR
  history; the results do not transfer for free to a repo whose reviews are all thumbs-up emoji.
- Not real-time. Ingestion is batch and offline; there is no incremental webhook pipeline.
- Not a benchmark of LLM quality. The gate that matters here measures **retrieval**, and it runs
  with zero LLM calls.
- Not multi-user or production-hardened. One developer, one laptop, one container.

---

## Documentation

Start with the roadmap; it says what to build next and, more usefully, what not to build.

| | |
|---|---|
| [docs/ROADMAP.md](docs/ROADMAP.md) | The 14-week plan: scope verdict, the nine decisions, six ranked traps, the week-by-week gates, the cut list, the novelty claim, the money model |
| [docs/tracker/](docs/tracker/README.md) | Where the work is tracked: one file per subsystem, plus the review-remediation backlog. The roadmap says *when*; the trackers say *what state it is in* |
| [docs/LEARNING-AGENDA.md](docs/LEARNING-AGENDA.md) | The five concepts this project lives or dies on, week by week, with the misconceptions each one prevents |
| [docs/ADR-0001-typed-edges.md](docs/ADR-0001-typed-edges.md) | Why typed edge tables rather than one polymorphic `edges` table |
| [docs/ADR-0002-golden-set-construction.md](docs/ADR-0002-golden-set-construction.md) | Why the anchor symbol must come from the code and never from the rationale text |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Starting everything, the degradation table, how to read a trace |
| [docs/DATA-PROTECTION.md](docs/DATA-PROTECTION.md) | What personal data is processed, and what is deliberately excluded |
| [provenance/eval/README.md](provenance/eval/README.md) | Why the CI gate splits in two, and why that half costs $0.00 |

## Quickstart

```bash
git clone <this repo> ~/pace && cd ~/pace
uv sync --extra dev              # base install is CPU-only and CUDA-free
make db-up                       # paradedb container, pinned by digest
make migrate                     # raw-SQL Alembic revisions
make trace-up                    # Jaeger, UI on http://localhost:16686
make skeleton                    # one request -> one complete trace, trace_id in the body
```

Then the two measurements the project lives or dies by:

```bash
make eval-retrieval              # retrieval gate: zero LLM calls, zero embedding calls
make leakage-probe               # substring baseline; must read below 0.35 recall@10
```

Everyday loop: `make fmt`, `make lint`, `make typecheck`, `make test`.

The corpus lives **outside** the repo:

```bash
git clone https://github.com/apache/airflow ~/corpus/airflow
pace scope                       # write the committed scope selection
pace ingest                      # walk history into Postgres
```

GitHub discussion text is *not* committed here - see "Licensing and the comment dump" below.

---

## Measured environment

These are measured facts about the machine this was built on, not aspirations. Design decisions
throughout the project follow from them.

| | |
|---|---|
| OS | Ubuntu 24.04 under WSL2; project on ext4 at `~/pace` |
| CPU / RAM | Intel i3-1115G4, 2 physical cores / 4 threads, 7.7 GB (WSL sees ~3.7 GB) |
| GPU | none - **no CUDA** |
| Python / uv | 3.12.3 / uv 0.11.16 |
| git / Docker | 2.43.0 / 29.1.3, **compose plugin not installed** |
| Database | `paradedb/paradedb@sha256:e4e80f24...93f4ed`, container `pace-db`, `localhost:5432`, `postgres`/`pace`, db `pace` |
| Server versions | PostgreSQL **18.6**, pgvector 0.8.4, pg_search 0.25.7 |

Consequences that are baked into this repo:

- **No compose file.** `make db-up` and `make trace-up` are plain `docker run`.
- **PG18 mounts its data volume at `/var/lib/postgresql`**, not `/var/lib/postgresql/data`. Using
  the older path gives a container that starts fine and loses the database on removal.
- The spec says Postgres 16; we are on 18.6. That is fine - it is stated once here and everything
  else is written against 18.6.
- **No torch in the base install.** On Linux the default PyPI torch wheel pulls 1.5-2 GB of CUDA
  packages onto a machine with no NVIDIA GPU. Embedding/reranking deps live in the `ml` extra, and
  `pyproject.toml` pins torch to `https://download.pytorch.org/whl/cpu`.

### Tokenizer behaviour (drives the index design)

The `pg_search` code tokenizer splits identifiers. Verified on this exact image:

```sql
SELECT 'let my_variable = 2;'::pdb.source_code::text[];        -- {let,my,variable,2}
SELECT '_normalise_url camelCaseName'::pdb.source_code::text[]; -- {normalise,url,camel,case,name}
```

**The whole identifier does not survive tokenization** - only its parts. So a search for
`_normalise_url` cannot match on the split tokens alone. The same column is therefore indexed
twice, `content::pdb.source_code` for split tokens and `content::pdb.literal` for the exact form,
and ingestion additionally emits an underscore-free joined form (`normaliseurl`) into the lexical
token array.

---

## The four things this scaffold exists to make possible

1. **One trace per request, trace_id in the response body.** A hello-world request produces a
   single complete OpenTelemetry trace, and the response carries its `trace_id`. Highest-leverage
   debugging decision in the project: every bad answer can be replayed span by span.
2. **Golden evidence is keyed on durable IDs, never on `chunk_id`.** Gold records reference
   `commit_sha` / `pr_number` / `issue_number` / `review_comment_id` / `(path, qualified_name)`,
   and `resolve_anchors()` maps those to today's chunk_ids at scoring time. This exists before a
   single row is ingested, which is what lets the eval survive a chunker change.
3. **The retrieval gate makes no network calls.** Frozen query embeddings are committed as a
   `.npz`. A cache **miss fails loudly** and never falls back to a live call - a silent fallback is
   how a CI job quietly starts costing money and quietly stops being reproducible.
4. **A leakage probe.** It measures what plain substring search scores on the golden set. Measured
   reality: a naively built golden set gives grep **recall@10 = 0.986** - the questions leak their
   answers. If the probe does not read **below 0.35** on the retained set, the benchmark measures
   string overlap rather than retrieval, and the project is invalid.

---

## Identity and reproducibility

`chunk_id` is sha256 over `scheme`, `repo`, `path`, `qualified_name` and normalized content, joined
by NUL bytes and defined once in `provenance/graph/ids.py`. `chunking_strategy_version`,
`embedding_model_version` and `prompt_version` are **columns**, never hash inputs - folding them in
would invalidate every chunk_id, and therefore every golden-set anchor, on the first chunker tweak.
Python's builtin `hash()` is never used; it is salted per process.

For the same reason `.gitattributes` sets `* -text`. `core.autocrlf` is `true` on the Windows git
used for editing and unset in WSL and CI, and that difference alone changes fixture bytes, changes
chunk_ids, and makes an eval pass locally while failing in CI for no visible reason.

## Configuration

All runtime knobs live in `provenance/config.py` (`pydantic-settings`, env prefix `PACE_`), and no
other module reads the environment. Override anything via `.env` or the environment, e.g.
`PACE_DATABASE_URL`, `PACE_CORPUS_PATH`, `PACE_OTEL_ENDPOINT`.

## Licensing and the comment dump

This repo is Apache-2.0. The **raw GitHub issue, PR and review-comment text is not** - it is written
by thousands of Airflow contributors and is their copyright, not covered by the ASF licence on the
source tree. So the dump is gitignored. What is committed: the IDs, the fetch script, and derived
numeric artifacts. Anyone reproducing this re-fetches the bodies themselves with `pace ingest`.

## CLI

```
pace demo       walking skeleton request, end to end
pace ingest     walk git history into Postgres
pace scope      select the subset of the repo under study
pace eval       run an eval suite
pace leakage    substring-search leakage probe over the golden set
pace version    installed version + derivation versions in effect
```

Every sub-command imports its implementation lazily, so `pace --help` works on a fresh clone with
optional dependencies missing.

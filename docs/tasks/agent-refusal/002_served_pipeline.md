# Task 002: The served pipeline — one warm process that retrieves, reranks, answers or declines, checks, and logs every query

**Tracker:** [007_AGENT_REFUSAL](../../tracker/007_AGENT_REFUSAL.md) tasks 1 and 5;
[000](../../tracker/000_REVIEW_REMEDIATION.md) findings 15 and 17 · **checkpoint D**, slice D2
**Priority:** P0 · **Status:** ⬜ Not started
**Decision records:** [ADR-0006](../../ADR-0006-refusal-first-checkpoint-d.md) §1 and its addendum
of 2026-11-17, in this spec's commit; [DATA-PROTECTION.md](../../DATA-PROTECTION.md) §6, updated in
the same commit
**Depends on:** [agent-refusal/001](001_unanswerable_questions.md) (the negatives and the hiding
contract); [eval-harness/007](../eval-harness/007_dense_and_fusion.md) (the fused retrieval)

## User Story

- **Summary:** Replace `/ask`'s stubs with the pipeline claim 5 is measured on. One warm process
  retrieves from the rationale pool, reranks, asks `gpt-6-luna` for an answer that cites its
  documents or declines, checks the answer's grounding, and logs every query from its first call.
  `pace ask` and the evaluation both go through it.

### Use Case

- **As the** author testing claim 5 and demonstrating PACE
- **I want** the evaluated system and the served one to be the same process, with every query's
  scores logged
- **so that** D3 measures what the demo shows, and finding 15's comparison can be made afterwards.

### Background (measured 2026-11-17)

- **The skeleton.** `provenance/api/main.py` serves `POST /ask` and `GET /healthz`. Every pipeline
  stage is a stub span, from `pace.parse_query` to `pace.verify`, and `/ask` always refuses. No LLM
  code exists, and `openai` is not installed.
- **Retrieval.** BM25 and exact dense search over the pool, each kept to its top 100 and fused by
  RRF at k = 60: B5b's best row, recall@10 0.159 on dev
  ([results/ablation.json](../../../results/ablation.json)). There is no reranker, by design
  (`provenance/retrieve/__init__.py`). On this machine MiniLM (`ms-marco-MiniLM-L-6-v2`) takes
  0.63 s at depth 10 and 3.57 s at depth 50 once loaded, and 17.4 s to load. fastembed 0.8.0,
  installed, serves it as `Xenova/ms-marco-MiniLM-L-6-v2` through onnxruntime.
- **The machine.** 2 cores, 4 GB of RAM, no GPU: no local LLM, and no torch-based grounding
  checker.
- **The pool.** 69,769 documents. Every one links to its commit through `chunk_evidence`. Every
  document of a pull request links to it too: 5,961 descriptions, 38,026 review comments and
  18,972 conversation comments, with 5,963 of the 6,810 commit messages.
- **The model.** OpenAI's `gpt-6-luna`: $0.10 per million input tokens, $0.01 cached and $0.50
  output on OpenAI's pricing page on 17 Nov 2026; Batch and Flex cost half. OpenAI's
  structured-outputs guide parses a response into a Pydantic model with
  `client.responses.parse(..., text_format=Model)`, read from `response.output_parsed`. Whether
  `gpt-6-luna` supports it is not confirmed: its model page did not load.
- **OpenAI's data terms** (its your-data guide, 17 Nov 2026). API data is not used for training
  unless the customer opts in. Abuse-monitoring logs are kept for up to 30 days. A Responses API
  response that is stored is kept for 30 days.
- **The questions.** D1's file holds 900 dev negatives, 300 of each of S3, S4 and S5. Of the 4,206
  retained dev answerables, 300 are held back as S3's sources.

## Design

### Units

| Unit | Kind | Responsibility |
|---|---|---|
| `provenance/llm/client.py` (new) | network, database | OpenAI's Responses API with structured output and `store=false`; the recordings; the running cost, its cap and the price table |
| `provenance/llm/prompts.py` (new) | pure | The answer and check prompts, versioned, and their output models |
| `provenance/llm/rules.py` (new) | pure | A cited label must be one shown; a quote must appear in its document |
| `provenance/retrieve/rerank.py` (new) | model | MiniLM through fastembed, loaded once |
| `provenance/api/pipeline.py` (new) | database, network | The six steps, the decision, the log row |
| `migrations/versions/0012_…` (new) | | The tables `llm_calls` and `ask_log` |
| `provenance/eval/pilot.py` (new) | read-only on the database, HTTP | `pace eval pilot` |
| `provenance/api/main.py`, `api/schemas.py`, `cli.py`, `config.py`, `pyproject.toml` | | `/ask` through a pipeline built at startup; the request's hide lists and ids; `pace serve`, `pace ask`, `pace eval pilot`; the key, model, cap and mode; `openai` as a dependency |

### The model check

Before any pipeline code, one call asks `gpt-6-luna` for a structured output. If it cannot give
one, the choice of model returns to the author.

### The process

`pace serve` runs the API with one uvicorn worker on 127.0.0.1. At startup it builds the pipeline
once: the query embedder, MiniLM, both retrievers and the OpenAI client. `pace ask "<question>"`,
`pace eval pilot` and D3's runner send questions to it over HTTP, and nothing else builds a
pipeline. `/healthz` reports whether the reranker is loaded, the LLM's mode and the spend so far.

### `/ask`, step by step

Each step fills a span of the existing trace; `pace.agent.step` stays a stub, since the loop is
future work. The pipeline reads only the question's text, and the router's label is logged, not
followed: every question is answered from the fused pool retrieval.

1. **Retrieve.** The question is embedded with the pool's model (bge-small). BM25 and exact dense
   search each return their top 100, and RRF at k = 60 fuses them. Documents that `chunk_evidence`
   links to a hidden commit or pull request are dropped, and the first 30 remain.
2. **Rerank.** MiniLM scores the 30 against the question. The best 8 are shown, labelled D1 to D8
   in rank order, each with its kind, its id and its text cut to 2,000 characters. Author logins
   are not shown.
3. **Answer.** One call with the question and the eight documents. The output is either a decline
   with a one-line reason, or one to five sentences, each citing one or more labels with a quote
   of at most 200 characters copied from the document.
4. **Rules.** Every cited label must be one of the eight, and every quote must appear in its
   document, runs of whitespace aside. A failure refuses the answer as `ungrounded`, with no check
   call.
5. **Check.** A second call with the question, the answer and only the documents it cites. The
   output is a verdict (`supported`, `partly_supported` or `unsupported`) and a support score from
   0 to 100.
6. **Decide.** The answer is refused when the model declined, a rule failed, the support score is
   below the threshold, or a call could not be made. The threshold is 50 in D2, provisionally: it
   serves the demo and the pilot and decides nothing. D3's addendum fixes the operating point.

The reply is `AskResponse`: the answer's sentences, citations as the cited documents' commits and
pull requests, the refusal and its reason, the cost and the trace id.

### Recordings

- Every call is recorded in `llm_calls`: the SHA-256 of the canonical request (model, prompts,
  output schema and settings, keys sorted), the request, the response, its cost and its time.
- **The default mode** answers an identical request from its recording and calls OpenAI only on a
  miss. **Replay mode** never calls, and a miss refuses the question as `llm_unavailable`. A rerun
  of the same questions on the same index therefore writes the same bytes.
- The key travels in a header, never in a recorded request.

### Budgets

- Each call has an output limit and a 60-second timeout, and the SDK retries transient errors.
- The server keeps a running cost from the usage each response reports, at `gpt-6-luna`'s prices,
  held in a table in the code with the date they were read. Once the cost reaches the cap
  (`PACE_LLM_BUDGET_USD`, $5 per server start by default), no call is made: the question is refused
  as `budget_exceeded` and logged, not retried (ROADMAP §8). Replayed calls cost nothing.

### The per-query log (finding 15)

From the first call, every `/ask` writes one `ask_log` row, refused or not. A row holds:
- its run id, the question's id when the caller gives one, the time, the router's label, and how
  many documents hiding removed;
- the fused scores at ranks 1 and 10, the rerank scores at ranks 1 and 8, and the eight documents
  shown;
- whether the model declined, whether the rules held, the cited documents and the commits and pull
  requests they link to, the verdict, the support score and the threshold applied;
- the outcome (`answered`, `declined`, `ungrounded`, `unsupported`, `budget_exceeded`,
  `llm_unavailable` or `llm_error`, the last with its kind: an API error, a safety refusal or an
  output that does not parse), the cost, each step's time, the model, the prompt version, the index
  version, the trace id and the two calls' request hashes.

It holds no question, answer or document text. Those stay in `llm_calls`, in the local database.

### Hiding

`AskRequest` takes `hide_commits` and `hide_prs`, which only the evaluation sends: an S3
question's `held_out` evidence. Step 1 drops every document linked to one of them, so the
question's commit message, its pull request's description and every comment on that pull request
are never shown. This is the contract [agent-refusal/001](001_unanswerable_questions.md) states.

### The pilot

`pace eval pilot` sends 50 dev questions through the running server: 20 retained answerables that
are not S3 sources, and 10 each of S3, S4 and S5, chosen by the SHA-256 of `"pace-pilot-v1:" +
qid`. S3 questions carry their hide lists. It writes `results/answer_pilot.json`: per kind, the
outcomes; for the answerables, how many answers cite a gold commit or pull request; the cost; the
median and 95th-percentile times. It holds ids and numbers only, reads no test question, and
decides nothing.

### Refusals

- `pace serve` does not start without a key, unless in replay mode, or before migration 0012.
- `pace ask` and `pace eval pilot` say so when no server answers.

### Not in this slice

- The operating point, the evaluation sample, the full dev and test runs, the risk–coverage curve
  and claim 5's verdict (D3).
- The Batch API: about $3 a run does not need it.
- The five-tool loop and HEAD code chunks ([ADR-0006](../../ADR-0006-refusal-first-checkpoint-d.md)
  §1).

## Acceptance Criteria

- **Scenario:** the decision record precedes the code
  - **Then:** ADR-0006's addendum and DATA-PROTECTION.md §6 are in this spec's commit, before any
    D2 code and before any call to OpenAI.

- **Scenario:** one warm process
  - **Then:** the server builds its pipeline once, and a second `/ask` loads no model.

- **Scenario:** the six steps
  - **Then:** on a fixture with fake models, a supported answer comes back with its commits and
    pull requests; a decline is refused as `declined`; a label not shown, or a quote not in its
    document, is refused as `ungrounded` with no check call; a support score below the threshold
    is refused as `unsupported`.

- **Scenario:** hiding
  - **Then:** no document shown for a question links to its hidden commits or pull requests.

- **Scenario:** the log
  - **Then:** every `/ask`, answered or refused, writes one `ask_log` row, and no row holds
    question, answer or document text.

- **Scenario:** recordings and budgets
  - **Then:** an identical request is sent once; replay mode never calls OpenAI; once the cap is
    reached, the next question is refused as `budget_exceeded` without a call; every request
    carries `store=false`.

- **Scenario:** determinism and the test split
  - **Then:** the pilot, rerun on its recordings, writes the same bytes, and no D2 command reads a
    test question.

## Files to Modify

- In the spec commit: `docs/ADR-0006-refusal-first-checkpoint-d.md` (addendum),
  `docs/DATA-PROTECTION.md` (header, §5, §6, §7), trackers 007 and 000 (findings 15 and 17)
- New: the units above
- Tests: unit (the rules, the output models, the decision, the budget, the recording keys);
  integration (the tables, recordings with a fake client, hiding on a fixture pool, the log row,
  `/ask` through FastAPI's test client with fakes); the CLI surface. No test calls OpenAI or loads
  MiniLM.
- `results/answer_pilot.json` (new, committed)
- Later: tracker 007, tracker 000, ROADMAP, RUNBOOK, `results/README.md`

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    uv run pace serve                      # loads once, then serves on 127.0.0.1
    uv run pace ask "<question>"           # in a second terminal
    uv run pace eval pilot                 # about 5 cents; a rerun writes the same bytes

# Task 004: Fetch the text of the pool's PRs under a request budget, and add it to the pool

**Tracker:** [006_EVAL_HARNESS](../../tracker/006_EVAL_HARNESS.md) · **checkpoint B**, slice B4 of 5;
closes review findings 10 and 11 ([000](../../tracker/000_REVIEW_REMEDIATION.md)) and
[001_INGEST](../../tracker/001_INGEST.md) row 2
**Priority:** P0 · **Status:** ✅ Done (2026-10-31)
**Decision records:** [ADR-0003](../../ADR-0003-rationale-pool-benchmark.md) §1–§2 and its second
2026-10-29 addendum; [DATA-PROTECTION.md](../../DATA-PROTECTION.md), whose §7 is the dated ethics
self-assessment
**Depends on:** [002](002_commit_message_pool.md) (the PR links) and
[003](003_golden_set_generator.md) (the generator and the verdict, re-run on the grown pool)

## User Story

- **Summary:** Fetch the description, inline review comments and conversation comments of the
  5,963 PRs that slice B2 linked to in-scope commits, at a request cost bounded per PR. Store them
  as DATA-PROTECTION.md requires, add the human-written text to the benchmark's pool, and
  regenerate the golden set and its leakage verdict against the grown pool.

### Use Case

- **As the** author defending checkpoint B's benchmark
- **I want** PR text in the pool, fetched once, cheaply and lawfully, with every request counted
- **so that** "where does the *why* live?" reaches past commit subjects, and "was the ethics
  self-assessment done before you collected people's comments?" and "what did the fetch cost?"
  both have committed answers.

### Background (measured 2026-10-29; the census is the authority)

- **The PRs.** B2 links 5,963 in-scope commits to 5,963 distinct PRs, numbered #5 to #72,861
  and merged from 2015 to 2026 (1,114 of them in 2025). PRs and issues share one numbering, so
  the repository holds roughly 12× more of them than the pool needs.
- **The schema exists, and is empty.** Migration 0003 created `pull_requests`, `issues`,
  `issue_comments`, `review_comments`, `edge_commit_pr` and `edge_pr_issue`; no row has been
  written. Two details shape the design:
  - `issue_comments` references `issues`, so a PR's conversation comments need an `issues` row
    for the PR;
  - each table has a `raw` column for the untouched API response. That response carries
    avatars, profile URLs and unscrubbed text, all of which DATA-PROTECTION.md §2 says are never
    stored.
- **The old fetcher was never run, and should not be.** `scripts/fetch_corpus.py` lists every PR,
  issue and comment in the repository, keeps identifiers only, then requests every body again,
  one at a time. Review finding 10 estimated that design at ~29 h of rate-limited fetching. Its
  manifest path, `corpus/manifest.jsonl`, is gitignored, so the manifest DATA-PROTECTION.md §3
  and §5 describe has never existed.
- **The ethics self-assessment had no date** (finding 11). DATA-PROTECTION.md §7 is the
  self-assessment; the commit that adds this spec dates it, before any request is made.
- **Decisions made with the user, 2026-10-29:**
  - §7 is the self-assessment, and needs only a date;
  - Claude runs the fetch, reading the user's token from the gitignored `.env`;
  - the pool gains each PR's description, inline review comments and conversation comments;
    issues stay out, as ADR-0003 §1 has it;
  - one PR at a time over REST, not by listing the whole repository (~12× more people's text
    than needed) and not through GraphQL (query-cost accounting and nested paging).

## Design

### Units

| Unit | Kind | Responsibility |
|---|---|---|
| `provenance/ingest/github_client.py` | HTTP | One authenticated REST client: pagination, the remaining-requests header, waits and backoff, a request counter. The token is a `SecretStr` and never appears in a message. |
| `provenance/ingest/github_backfill.py` | DB + HTTP | The ethics guard; which PRs; one PR's requests and its storage in one transaction; the run record; the manifest. `pace github backfill`. |
| `provenance/ingest/pr_pool.py` | DB, then pure | The PR documents and their evidence, written by `pace pool build` after the commit messages, under B2's kind of invariants. |
| `migrations/versions/0009_github_bots.py` | DDL | `author_is_bot` on `pull_requests`, `review_comments` and `issue_comments`. |
| `provenance/analysis/pool_census.py` | DB | A `github` section in `results/pool_census.json`. |

### The fetch

- **The guard.** `pace github backfill` refuses to start, before its first request, unless
  DATA-PROTECTION.md §7 carries a line `**Self-assessment completed:** YYYY-MM-DD` dated no later
  than today.
- **Which PRs.** The PR numbers of B2's `linked` evidence: 5,963.
- **The requests for one PR**, 100 items per page:
  1. `GET /repos/apache/airflow/pulls/{n}`: title, description, state, merge commit, and the
     counts of inline review comments and conversation comments;
  2. `GET …/pulls/{n}/comments`, only if the review-comment count is above zero;
  3. `GET …/issues/{n}/comments`, only if the conversation count is above zero.

  Pages follow GitHub's `Link` header. **The request budget is that bound:** at most
  1 + ⌈review comments / 100⌉ + ⌈conversation comments / 100⌉ requests per PR, so the whole
  fetch costs between 5,963 and roughly 18,000 requests — 1 to 4 hours at GitHub's 5,000 an hour.
  `--max-requests` (default 20,000) stops the run between PRs, never inside one, and `--plan`
  prints the PRs left and the minimum cost without making a request.
- **Rate limits.** The client reads `x-ratelimit-remaining` and, at zero, waits until
  `x-ratelimit-reset` rather than drawing a refusal. A secondary-limit refusal (403 or 429) waits
  `retry-after`, else backs off exponentially; a 5xx backs off and retries. A 401 stops the run
  with a message that names the setting, never the token.
- **Storage, one transaction per PR**, so a PR is stored whole or not at all:
  - a `pull_requests` row. `state` is `merged` when `merged_at` is set. `raw`,
    `author_association` and `head_ref` stay empty: the last two reveal organisation membership
    and, for a fork, a person's name;
  - an `issues` row marked `is_pull_request`, carrying no text and no login. It exists because
    `issue_comments` references it, and the PR's text is stored once, in `pull_requests`;
  - its `review_comments` (anchored on `original_line` and `original_commit_sha`, as migration
    0003 requires; `is_resolved`, `is_outdated` and `thread_node_id` stay NULL, being
    GraphQL-only) and its `issue_comments`;
  - an `edge_commit_pr` row with source `merge_commit_sha` when GitHub's merge commit is a
    commit of the graph — GitHub's own record of which commit landed the PR.
- **Data protection.** Every text field (title, description, comment, diff hunk) passes
  `scrub_emails()` before storage. From GitHub's `user` object, only `login` and whether the
  account type is `Bot` are kept. No commit data is fetched, so no author address is ever
  received.
- **Resumable.** A PR already in `pull_requests` costs nothing on a later run. A number GitHub
  does not know as a PR (404, such as a `(#N)` that named an issue) or reports gone (410) is
  recorded as an outcome, not raised. Any other error ends the run `failed`; running the command
  again resumes it.
- **The run record.** The run is an `ingest_runs` row with driver `github_backfill`, kept through
  the git walk's `Checkpointer`, so a second concurrent run is refused the same way. Its
  `checkpoint` holds the requests made, the PRs fetched and skipped, the outcomes, and the
  rate-limit waits.
- **The manifest.** At the end of every run the backfill writes `results/github_manifest.jsonl`
  from the database: one line per stored artifact — kind, PR number, comment id, and the SHA-256
  of the stored, scrubbed text — sorted, with no text and no login. Anyone fetching again later
  can count how many bodies were edited since.
- **The old fetcher is deleted.** `scripts/fetch_corpus.py` goes, and with it the 29-hour design.

### The documents (ADR-0003 §1–§2 and the second 2026-10-29 addendum)

| Document | `kind` | `path` | Text |
|---|---|---|---|
| PR description | `pull_request` | `pull/<n>` | title, a blank line, and the description with its HTML comments removed |
| Inline review comment | `review_comment` | `pull/<n>/review/<id>` | the comment |
| Conversation comment | `issue_comment` | `pull/<n>/comment/<id>` | the comment |

- **Never a document:** text whose author is a bot account (`author_is_bot`), and a comment that
  is empty once stripped. A welcome or stale-bot message would otherwise count as gold for every
  PR it appears on.
- **HTML comments** are removed from descriptions only: they hold the PR template's instructions,
  which GitHub never displays.
- **Evidence.** Each document is `self` evidence for its own artifact (`pull_request` *n*,
  `review_comment` *id*, `issue_comment` *id*) and `linked` evidence for its PR and for the
  commit B2 linked to that PR. Every document of a change therefore satisfies both of a record's
  units, the commit and the PR (ADR-0003 §4); each PR still has exactly one commit in the pool,
  so a record still scores 0 or 1.
- **Rows.** As B2's: `language = 'text'`, `qualified_name = NULL`, lines 1..*n*,
  `first_seen_commit` and `last_seen_commit` the linked commit, `chunking_strategy_version =
  'pull-request-v1'`, and the chunk id from `graph.ids.chunk_id` over the path and the normalised
  text.
- **Invariants**, checked in the build's transaction: no PR document belongs to a PR outside
  B2's links, and no artifact has two documents (a stale document from an earlier build). Either
  failure writes nothing.
- **`pace pool build`** writes the commit messages, then the PR documents; a second run inserts
  nothing.

### The census

`results/pool_census.json` (`pace measure pool`) gains a `github` section, counts and identifiers
only:

- PRs linked, fetched, and not found as PRs;
- documents per kind, and the texts left out (bot-authored, empty);
- B2's subject links checked against GitHub's merge commits: agreeing, pointing elsewhere, merge
  commit outside the graph, no merge commit;
- the fetch: runs, requests made, requests per PR, rate-limit waits, first and last fetch date;
- the self-assessment date read from §7, beside the first fetch date.

### After the fetch

- **The real run, in order:**
  1. a smoke run with `--max-requests 50`, checked by hand against github.com;
  2. the full run, in the background;
  3. `pace pool build` and `pace measure pool`.
- **The golden set is regenerated.** The 8,250 questions do not change, because they come from
  the code diff. Every leakage fact is measured again against the grown pool, and gold now
  includes the PR's documents. Until then the verdict reads INCONCLUSIVE: B3's stale-fact check
  refuses to pass an old set against a new pool. Each record's `probe_index_version` counts the
  pool's documents by kind, replacing `commit-message-v1:6810`.
- **The verdict is re-run, and committed whatever it says.** PR descriptions often name the
  function they change, so more questions may share a rare token with their gold and be dropped,
  and BM25 may find more of the rest: the verdict may move towards FAIL, or retention towards
  ADR-0003 §6's 25% floor. The gate was fixed before the data existed, so its answer is reported
  either way. If it is not PASS, work stops for the user's decision.

### Not in this slice

- **Issues and PR→issue edges** (`edge_pr_issue`): outside B's pool (ADR-0003 §1).
- **Review summaries** (`/pulls/{n}/reviews`) and **review-thread resolution state**
  (GraphQL-only).
- **Refreshing edited bodies.** The manifest makes drift measurable; re-fetching is a later
  decision.
- **The gold-validity audit** (review finding 6). It reads a record's gold text, which now
  includes PR text, so it follows this slice.

## Acceptance Criteria

- **Scenario:** no fetch before a dated self-assessment
  - **Given:** DATA-PROTECTION.md with no completion line, with a future date, and with today's
  - **Then:** the first two refuse before any request; the third proceeds.

- **Scenario:** a PR costs what the bound says
  - **Given:** a fake GitHub serving one PR with no comments, and one with 150 review comments
    and 3 conversation comments
  - **Then:**
    - the first costs 1 request and the second 1 + 2 + 1;
    - `--max-requests` stops between PRs, never inside one;
    - `--plan` makes no request.

- **Scenario:** rate limits are waited out, and the token never leaks
  - **Given:** a response with `x-ratelimit-remaining: 0`, a 429 with `retry-after`, a 503, and a
    401
  - **Then:**
    - the first waits until the reset, by a fake clock;
    - the second waits `retry-after` and the third backs off, both then retrying;
    - the fourth stops the run, and its message does not contain the token.

- **Scenario:** what is stored, and what is not
  - **Given:** a PR whose description and a comment contain email addresses, a bot-authored
    comment, and API payloads carrying avatars, `author_association` and `head.ref`
  - **Then:**
    - every stored text field is scrubbed;
    - `raw` is `{}`, and `author_association` and `head_ref` are NULL;
    - the bot comment is stored with `author_is_bot`;
    - each PR has an `issues` row with no text and no login.

- **Scenario:** the fetch, end to end on `history_repo`
  - **Given:** the fixture ingested, its pool built, and a fake GitHub serving PRs #11–#14, one
    of which answers 404
  - **When:** the backfill runs twice
  - **Then:**
    - the first run stores three PRs, their comments, a `merge_commit_sha` edge where the merge
      commit is in the graph, and a run record with its request count;
    - the 404 is an outcome;
    - the second run makes no request and writes nothing;
    - the manifest is identical after both runs.

- **Scenario:** PR text joins the pool
  - **Given:** the fetched fixture PRs
  - **When:** `pace pool build` runs, twice
  - **Then:**
    - the documents have the paths and kinds above, with descriptions free of HTML comments;
    - the bot comment and an empty comment have no document;
    - each document's evidence is itself, its PR and its linked commit;
    - the resolver finds both units of a record across every document of the change;
    - the second build inserts nothing;
    - a stale PR document makes the build fail and write nothing.

- **Scenario:** the census tells the fetch's story
  - **Then:** it counts PRs, documents per kind and texts left out; it reports the cross-check
    against merge commits and the requests per PR; and its self-assessment date is no later than
    its first fetch date.

- **Scenario:** the real fetch, the regenerated benchmark, and the verdict
  - **Given:** `pace`, with the self-assessment committed
  - **Then:**
    - a 50-request smoke run matches github.com for the PRs it fetched;
    - the full run stays within its budget, and its requests per PR are in the census;
    - `pace golden generate` and the verdict reproduce byte for byte across two runs;
    - `results/leakage_verdict.json` is committed with whatever verdict it gives.

- **Scenario:** the plan documents agree
  - **Then:**
    - DATA-PROTECTION.md's dated §7 and ADR-0003's addendum are committed before the first fetch;
    - nothing names `scripts/fetch_corpus.py`;
    - RUNBOOK describes `pace github backfill`;
    - `results/README.md` lists the manifest;
    - ROADMAP shows B4;
    - tracker 000 rows 10 and 11 and tracker 001 row 2 are ✅.

## Files to Modify

- `provenance/ingest/github_client.py`, `provenance/ingest/github_backfill.py`,
  `provenance/ingest/pr_pool.py`, `migrations/versions/0009_github_bots.py`: new
- `scripts/fetch_corpus.py`: deleted
- `provenance/config.py` (`github_token`), `provenance/cli.py` (`pace github backfill`),
  `provenance/ingest/message_pool.py` (`pace pool build` writes the PR documents too),
  `provenance/analysis/pool_census.py`, `provenance/eval/generator.py` (`probe_index_version`),
  `provenance/privacy.py` (docstring)
- `results/github_manifest.jsonl`: new; `results/pool_census.json`,
  `results/golden_census.json`, `results/leakage_verdict.json`,
  `provenance/eval/golden/auto_full.jsonl`: regenerated
- Tests: `tests/unit/test_github_client.py`, `tests/unit/test_github_backfill_rules.py`,
  `tests/unit/test_pr_pool_rules.py`, `tests/integration/test_github_backfill.py` (new);
  `tests/unit/test_privacy.py`, `tests/unit/test_cli_surface.py`,
  `tests/integration/test_schema.py`
- `docs/DATA-PROTECTION.md` and `docs/ADR-0003-rationale-pool-benchmark.md` (in the spec commit),
  `docs/RUNBOOK.md`, `docs/ROADMAP.md`, `docs/tracker/000_REVIEW_REMEDIATION.md`,
  `docs/tracker/001_INGEST.md`, `docs/tracker/006_EVAL_HARNESS.md`, `results/README.md`,
  `.gitignore` (comment)

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests -q
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    uv run alembic upgrade head
    uv run pace github backfill --plan
    uv run pace github backfill --max-requests 50      # the smoke run, then without the cap
    uv run pace pool build && uv run pace measure pool
    uv run pace golden generate                         # twice: both outputs byte-identical
    uv run pace leakage --from-db --no-dense --golden provenance/eval/golden/auto_full.jsonl \
        --json-out results/leakage_verdict.json

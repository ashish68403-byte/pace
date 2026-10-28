# Task 002: Commit messages become the benchmark's first pool

**Tracker:** [006_EVAL_HARNESS](../../tracker/006_EVAL_HARNESS.md) · **checkpoint B**, slice B2 of 5
**Priority:** P0 · **Status:** ✅ Done (2026-10-28)
**Decision record:** [ADR-0003](../../ADR-0003-rationale-pool-benchmark.md) §1–§2, §4
**Depends on:** [001](001_benchmark_definition.md) (migration 0007, relevance units) and
[ingest 004](../ingest/004_personal_data.md) (messages are scrubbed at the source)

## User Story

- **Summary:** Write the message of every commit that touches an in-scope file into `chunks` as a
  retrievable document, with `chunk_evidence` rows saying which commit it is (`self`) and which
  apache/airflow PR it belongs to (`linked`). After this slice, B1's resolver can turn a gold commit
  sha or PR number into documents, which is the precondition for slice B3's generator and for
  every retrieval number.

### Use Case

- **As the** author defending checkpoint B's benchmark
- **I want** the pool built by one idempotent command whose every link to a PR is right
- **so that** a gold PR number can never resolve to another PR's text, and the pool's size and
  link coverage are census figures anyone can reproduce.

### Background (measured 2026-10-27 on `pace`, corpus `1e2ad803f7`)

- **Nothing writes `chunks` or `chunk_evidence` yet.** The chunker builds `Chunk` objects in
  memory only, and both tables are empty. This slice is their first writer.
- **The pool is 6,810 commits**, the first-parent commits with a `file_revisions` row (ADR-0003
  §1). 501 are merges and 217 are mechanical; both stay in as distractors and are never gold.
- **PR references in subjects:**
  - 5,649 subjects end with GitHub's squash-merge `(#N)`;
  - 453 are merge commits worded `Merge pull request #N from owner/branch`;
  - none match both patterns, giving 6,102 candidate links;
  - 708 carry no reference.

  Where a subject holds two numbers (reverts, backports), the trailing one is the PR that
  produced the commit; the earlier one is quoted text.
- **Two PR numbering spaces.** `Merge pull request` numbers run from #3 to #66 in 2014, and up to
  #244 by 2015-06-05, on `mistercrunch/...` branches: Maxime Beauchemin's earlier repository.
  airbnb/airflow's numbering, which apache/airflow inherited, starts at #5 with commit
  `1f63537c9b1105d0c61335cafc49305e534969d8` (2015-06-07, `topo_order` 477). 138 candidate links
  precede it and name PRs in the other space.
- **One fork-internal reference.** On 2018-04-22, `[AIRFLOW-1314] Small cleanup to address PR
  comments (#24)` names a PR in a contributor's fork. Its 24 is 0.8% of the highest PR linked
  before it (3,150). After the boundary, the next-lowest ratio is 45% (#8231 in 2021, a PR that
  stayed open a long time).
- **Unfiltered, both patterns give 31 PR numbers on two commits.** Every one is a numbering
  collision (#10 in 2014 and #10 in 2015 are different PRs), not a PR that landed twice. With the
  two filters below, 5,963 links name 5,963 distinct PRs, and no PR has two documents.
- **ADR-0003's Context figure cannot be reproduced.** It says "6,440 distinct PR numbers". The
  filtered rule gives 5,963; unfiltered gives 6,071; any `#N` in the subject gives 6,111; any `#N`
  in the whole message gives 6,854.
- **Message length:**

  | | Median | p90 | p99 | Max |
  |---|---|---|---|---|
  | Words | 15 | 100 | 359 | 1,335 |
  | Lines | 3 | 17 | 66 | 227 |

  103 messages exceed 300 words.
- **Subjects:** 2 of 6,810 stored messages do not start with the stored subject, because git
  folds a wrapped first paragraph into one subject line. The document is the message; the subject
  is read only for PR references.
- **Pool filter missing:** `scripts/leakage_probe.py:load_pool_from_db` selects every live row of
  `chunks`, whatever its `kind`.
- **Unused prose path:** migration 0004's `embeddings_prose_*` tables accept a `commit_message:`
  `doc_ref`. Under ADR-0003 §1 commit messages are chunks, so that path stays unused.

## Design

### The pool writer: `provenance/ingest/message_pool.py`

Pure functions, then one database function:

- **`pr_reference(subject) -> int | None`.** `^Merge pull request #(\d+)` if the subject matches,
  else a trailing `\(#(\d+)\)\s*$`, else `None`.
- **`link_decisions(commits, *, numbering_starts_at) -> list[LinkDecision]`**, in walk
  (`topo_order`) order. Each commit gets one outcome:
  - **linked:** it has a reference, comes at or after the numbering boundary, and its number is
    at least `PR_NUMBER_FLOOR` (0.10) of the highest number linked before it;
  - **no reference;**
  - **dropped (before numbering):** the commit precedes the boundary;
  - **dropped (implausible):** the number is below the floor.

  Only linked numbers raise the running maximum.
- **`message_document(sha, message, *, repo_slug) -> MessageDocument`.** The fields:
  - `kind='commit'`, `path='commit/<sha>'`, `language='text'`, `qualified_name=None`;
  - `content = normalize_content(message)` (the stored message, already scrubbed by ingest 004);
  - `start_line=1`, `end_line` = its line count;
  - `chunk_id = ids.chunk_id(repo_slug, path, None, content)`;
  - `content_sha` = sha256 of the content;
  - `lexical_tokens = chunker.lexical_tokens(content)`, with `lexical_blob` their space-joined form;
  - `first_seen_commit = last_seen_commit = sha`;
  - `chunking_strategy_version = 'commit-message-v1'`.
- **`write_pool(conn, repo_id, *, numbering_starts=APACHE_NUMBERING_STARTS) -> PoolReport`.** Reads
  the pool in walk order. Writes one document per commit and one `self` evidence row
  (`commit`, full sha). For linked commits it adds one `linked` row (`pull_request`, the number).
  All of it happens in one transaction, with `ON CONFLICT DO NOTHING` on both tables. The report
  counts inserted and skipped rows per table, and each link outcome.

`APACHE_NUMBERING_STARTS = "1f63537c9b1105d0c61335cafc49305e534969d8"` is a named constant. Its
docstring carries the measurement above.

### Invariants

They are checked after writing, inside the same transaction, so a failure rolls everything back
and names the offending shas:

1. Exactly one `commit` document per pool commit. A message whose text changes (a new scrub rule,
   say) gets a new id, and would otherwise leave a stale second document behind.
2. No `commit` document outside the pool.
3. The boundary commit is in the walked graph. Without it the cutoff cannot be applied, so the
   build refuses rather than linking everything.

A PR with two documents is reported, not refused: ADR-0003 §4 allows the case.

### Commands

- `pace pool build [--json]`: a new `pool` sub-app, which leaves room for slice B4's GitHub sources.
- `pace measure pool`: `provenance/analysis/pool_census.py` writes `results/pool_census.json`. It
  holds counts only, no message text:
  - the corpus commit;
  - pool size, merges and mechanical commits;
  - the four link outcomes, with the shas of every dropped link;
  - distinct PRs, and PRs with 2+ documents;
  - percentiles of message words and lines, and the count over 300 words.

### One pool definition

`RATIONALE_KINDS = ("commit", "pull_request", "review_comment", "issue", "issue_comment")` lives in
`provenance/graph/tables.py`: ADR-0003 §1's pool, with B4's kinds named in advance.
`load_pool_from_db` adds `AND kind = ANY(%s)`, so checkpoint C's code chunks can never enter the
benchmark's pool.

### Not in this slice

- **Embeddings** (slice B5). Commit messages will embed through `chunks`, so through
  `embeddings_code_*`, never 0004's `embeddings_prose_*`. The prefix is documented as unused; no
  migration.
- **Splitting long messages.** ADR-0003 §1 sets one document per commit; the census counts those
  over 300 words for B5's 512-token window.
- **`ingest_runs` and dead-letter rows.** The build is a pure transform of rows the walk already
  validated. A failure is a bug, and the invariants raise it.

## Acceptance Criteria

- **Scenario:** a subject's PR reference is read the way GitHub wrote it
  - **Given:** real subject forms: a squash `… (#55405)`; a revert
    `Revert "X (#41856)" (#42193)`; `Merge pull request #10 from airbnb/fix_authenticate`;
    `Fix (#12) handling of X`; `see #12`; no number
  - **Then:** `pr_reference` returns 55405, 42193, 10, None, None and None.

- **Scenario:** only apache/airflow PR numbers are linked
  - **Given:** a synthetic walk with references before the boundary, after it, and one number below
    10% of the highest linked before it
  - **When:** `link_decisions` runs
  - **Then:**
    - the first are dropped (before numbering);
    - the low number is dropped (implausible), while a number at exactly 10% is linked;
    - numbers dropped before the boundary never raise the running maximum.

- **Scenario:** a document is built the same way every time
  - **Given:** a sha and a message with a trailing newline
  - **Then:** `message_document` gives `path='commit/<sha>'`, `kind='commit'`, `language='text'`,
    a normalized content, `end_line` equal to its line count, `chunk_id` equal to
    `ids.chunk_id("apache/airflow", path, None, content)`, and lexical tokens equal to the
    chunker's.

- **Scenario:** the pool is written with its evidence
  - **Given:** the hermetic `history_repo` ingested; 8 of its 9 first-parent commits touch an
    in-scope file, and 4 of them carry #11–#14
  - **When:** `write_pool` runs with the boundary at the root commit
  - **Then:** 8 documents, 8 `self` rows and 4 `linked` rows are written.
  - **And:** with the boundary at `c6`, #11 and #12 are dropped (before numbering), and only #13
    and #14 are linked.

- **Scenario:** the build is idempotent
  - **When:** `write_pool` runs a second time
  - **Then:** it inserts 0 rows and skips exactly what the first run inserted.

- **Scenario:** a broken pool fails, and leaves nothing behind
  - **Given:** an extra stale `commit` document for a pool commit, or a boundary sha missing from
    the graph
  - **When:** `write_pool` runs
  - **Then:** it raises with the offending shas, and the transaction writes nothing.

- **Scenario:** B1's resolver now finds documents
  - **Given:** the written fixture pool, and a record requiring commit `c7` and PR 14
  - **When:** `resolve_anchors` runs
  - **Then:** it returns two units, `("commit", c7)` and `("pr", "14")`, each holding `c7`'s
    message document.

- **Scenario:** the probe's pool is rationale text only
  - **Given:** the fixture pool plus one `kind='chunk'` row standing in for a code chunk
  - **When:** `load_pool_from_db` runs
  - **Then:** it returns the message documents and not the code chunk.

- **Scenario:** the real pool, measured
  - **Given:** `pace` with checkpoint A's graph and ingest 004 applied
  - **When:** `pace pool build` runs twice, then `pace measure pool` runs twice
  - **Then:**
    - run 1 writes 6,810 documents and 12,773 evidence rows (6,810 `self`, 5,963 `linked`);
    - link outcomes are 5,963 linked, 708 no reference, 138 dropped before numbering and 1
      dropped implausible;
    - 0 PRs have two documents;
    - run 2 inserts 0 rows;
    - `results/pool_census.json` is byte-identical across the two census runs.

- **Scenario:** the plan documents agree with the measurement
  - **Given:** ADR-0003, ROADMAP, tracker 006 and `results/README.md`
  - **Then:**
    - ADR-0003 carries a dated erratum. It gives the measured numbers in place of 6,440 and the
      numbering history, says §4's 0.5 case does not arise in the commit-message pool, and says
      commit messages embed through `chunks`. Its decisions are unchanged.
    - ROADMAP shows B1 and B2 done.
    - Tracker 006 closes B1's open item about PRs on two commits with this measurement.
    - `results/README.md` lists `pool_census.json`.

## Files to Modify

- `provenance/ingest/message_pool.py`, `provenance/analysis/pool_census.py`: new
- `provenance/graph/tables.py`: `RATIONALE_KINDS`
- `provenance/cli.py`: the `pool` sub-app with `build`; `measure pool`
- `scripts/leakage_probe.py`: the `kind` filter in `load_pool_from_db`
- `tests/unit/test_message_pool.py`, `tests/integration/test_message_pool.py`: new;
  `tests/unit/test_cli_surface.py`: the two new commands
- `results/pool_census.json` (new), `results/README.md`
- `docs/ADR-0003-rationale-pool-benchmark.md` (erratum), `docs/ROADMAP.md`,
  `docs/tracker/006_EVAL_HARNESS.md`

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests -q
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    uv run pace pool build --json      # twice: the second run inserts 0 rows
    uv run pace measure pool           # twice: results/pool_census.json byte-identical
    git diff --exit-code results/pool_census.json

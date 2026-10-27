# Task 001: Write the commit graph

**Tracker:** [001_INGEST](../../tracker/001_INGEST.md) · task 5 · **checkpoint A** (history graph +
truncation measurement), slice S1 of 3
**Priority:** P0 · **Status:** ⬜ Not started
**Measured at:** corpus commit `1e2ad803f74eadb5fb28d4504ca06b008c48b63e`

## User Story

- **Summary:** `pace ingest` walks 38,888 commits and stores none of them. Persist every walked
  commit and its parent links, and prove that a second run writes nothing.

### Use Case

- **As the** author of every measurement downstream of the graph
- **I want** `commits` and `commit_parents` populated from the full first-parent history
- **so that** file history ([002](002_file_history.md)) and the truncation measurement
  ([003](003_history_truncation.md)) have real rows to join against, and "re-running ingest changes
  nothing" is a measured fact rather than a claim.

### Background (measured)

Each number was produced by the command beside it, run in the corpus clone.

| Fact | Value | Command |
|---|---|---|
| First-parent commits | 38,888 | `git rev-list --first-parent --count HEAD` |
| All commits | 40,995 | `git rev-list --count HEAD` |
| First-parent merges | 1,031 | `git rev-list --first-parent --merges --count HEAD` |
| Merges with 3+ parents | 0 | `git rev-list --first-parent --min-parents=3 --count HEAD` |
| Parent links on the first-parent walk | 39,918 | `git rev-list --first-parent --parents HEAD \| awk '{n+=NF-1} END{print n}'` |
| Records from `raw_log_stream`'s exact git command | 38,888, all distinct, 10 s | the command in `git_walk.py::raw_log_stream`, piped to a counter |

So the walk carries 38,887 first-parent links (every commit except the root) and exactly 1,031
second-parent links, whose targets the walk never visits.

Three defects in the current code block the write path:

1. `walk_history` calls its hook only for commits that touch an in-scope file
   (`git_walk.py:1043`). Most of those commits' parents touch no in-scope file, so a parent link
   written from the hook would point at a row that is never written, and `commit_parents` has a
   foreign key on both ends.
2. The log format (`%H %P %an %ae %aI %ce %s %B`) reads no committer name, committer date or tree.
   `commits.committed_at` is `NOT NULL`, so no commit row can be built from what the walker reads.
3. `commits.insertions` / `deletions` and the same pair on `file_revisions` are
   `NOT NULL DEFAULT 0`, and nothing computes them. A stored 0 would read as "this commit changed no
   lines".

One documentation drift, found while measuring: the module docstring says "`git log --
airflow-core/src/airflow` reports **4,007** commits; the full walk reports **17,015**". Measured:
4,008, and 17,015 is `git rev-list --count HEAD -- airflow` — the legacy path, counted over all
parents. The full first-parent walk is 38,888; the legacy path on the first-parent walk is 15,844.

## Design

| # | Decision | Why | Rejected |
|---|---|---|---|
| 1 | Write **every** walked commit (38,888), not only in-scope ones | A parent link needs both ends to exist. "In scope" becomes a join through `file_revisions` (task 002), not a write-time filter | Writing only in-scope commits: most links would violate the foreign key, and `commit_parents` would stop being the real DAG |
| 2 | Write parent links **after** the walk, from links collected during it | The walk must run newest → oldest — `RenameChains` can only resolve an old path after it has seen the newer rename — so a commit's parent row is written one step after the commit | A `DEFERRABLE` foreign key inside one walk-long transaction (a crash loses everything, and it needs a migration); `git log --reverse` (breaks rename resolution); inserting links as you go (the foreign key rejects them) |
| 3 | `topo_order = total − position`, with `total` from `git rev-list --first-parent --count <rev>` and the newest commit at position 0 | The root is 1 and the newest is 38,888. The root never moves, so every number stays fixed when new commits arrive at the tip | Numbering from the tip (renumbers the table on every pull); ordering by `authored_at` (clock skew, and not unique) |
| 4 | `INSERT … ON CONFLICT (repo_id, commit_sha) DO NOTHING`, counting inserted vs already-present from `rowcount` | Idempotent re-runs. The conflict target is named so that a `topo_order` clash on `commits_topo_unique` raises instead of being counted as already present | A bare `ON CONFLICT DO NOTHING`, which swallows *any* unique violation — including the one that means the numbering is wrong |
| 5 | Each commit is written inside its own savepoint; the batch commits every 250 commits | A failing row rolls back only itself and becomes a `dead_letters` row. Without the savepoint Postgres aborts the whole transaction ("current transaction is aborted, commands ignored until end of transaction block") and every later row in the batch fails with it | One transaction per commit (38,888 fsyncs); one transaction per batch without savepoints (one bad row loses up to 249 good ones) |
| 6 | Crash recovery is "run it again" | Every write is idempotent, so a re-run skips what exists and finishes the rest. The checkpoint row reports progress; it is not a resume cursor | Resume-from-checkpoint: real complexity for a walk whose git side takes 10 s |
| 7 | Second-parent links are counted and reported, never stored | Their targets — 2,107 commits reachable only through a merge — are not on the first-parent walk, so the foreign key cannot hold. Their changes are still attributed, to the merge commit, through its first-parent diff | Walking every parent: it changes rename resolution, `topo_order` and diff attribution at once, for 5.1% of commits |
| 8 | Migration `0006` makes the four `insertions` / `deletions` columns nullable, with no default | NULL means "not measured". The repo already holds that an empty field beats a plausible fiction (header of `provenance/eval/golden/ci_subset.jsonl`) | Leaving the 0; computing `--numstat` per commit (more git work and parsing for numbers nothing reads) |

The walker stays a walker; persistence is a new module, `provenance/ingest/graph_writer.py`.
`walk_history` takes an optional `writer` in place of the `on_commit` hook — nothing calls the hook
(`grep -rn on_commit` outside `git_walk.py` finds nothing) — calls it for **every** commit, assigns
each record its `topo_order`, and passes the writer's insert/skip counts to the existing
`Checkpointer`, so `ingest_runs.rows_*` carry real numbers. The writer's per-table detail goes in
`ingest_runs.checkpoint`.

Counts mean one thing each:

- `rows_inserted` — rows this run created.
- `rows_skipped` — rows that already existed. Nothing else.
- `links_to_unwalked_parents` — links whose parent is not in `commits` (reported separately, because
  they are not rows at all).
- `links_failed` — links whose child or parent was dead-lettered in this run.

## Acceptance Criteria

- **Scenario:** a full ingest writes the whole first-parent history
  - **Given:** an empty `commits` table and the corpus at the pinned commit
  - **When:** `uv run pace ingest` completes
  - **Then:** `commits` holds 38,888 rows for the repository, `topo_order` runs from 1 to 38,888,
    and `commit_parents` holds 38,887 rows — one first-parent link per commit except the root.
  - **And:** the run reports `links_to_unwalked_parents = 1,031`.

- **Scenario:** a second run writes nothing, and proves it saw everything
  - **Given:** the state after the first run
  - **When:** `uv run pace ingest` runs again
  - **Then:** its `ingest_runs` row has `rows_inserted = 0` **and** `rows_skipped` equal to the first
    run's `rows_inserted`. "0 inserted" alone is also what a writer that writes nothing reports.

- **Scenario:** the run checks its own invariants
  - **Given:** any completed walk, full or `--max-commits N`
  - **When:** the writer finishes
  - **Then:** it asserts that every walked commit whose first parent is in `commits` has its
    first-parent link, and, on a full run, that the walk produced exactly
    `git rev-list --first-parent --count` commits. A violation fails the run
    (`status = 'failed'`); it is never only a log line.

- **Scenario:** one bad commit costs one row
  - **Given:** a commit whose insert fails
  - **When:** the walk continues
  - **Then:** that commit is in `dead_letters`, every other commit in its batch is written, and each
    link touching it is counted in `links_failed`.

- **Scenario:** a numbering clash is loud
  - **Given:** a different commit already holds the `topo_order` a new commit needs
  - **When:** the new commit is written
  - **Then:** the insert raises and is dead-lettered; it is never counted as already present.

## Files to Modify

- `provenance/ingest/git_walk.py` — the log format gains `%T %cn %cI`; `CommitRecord` gains
  `tree_sha`, `committer_name`, `committer_time`, `topo_order`; `walk_history` takes `writer` and
  assigns `topo_order`; the module docstring's 4,007 / 17,015 corrected
- `provenance/ingest/graph_writer.py` — new: commit rows, parent links, counts, invariants
- `provenance/graph/tables.py` — constants for `commit_parents`, `file_revisions`, `ingest_runs`,
  `dead_letters`
- `migrations/versions/0006_history_columns.py` — new; shared with task 002, which adds
  `file_revisions.path_at_commit` in the same revision
- `tests/unit/test_git_walk_parse.py`, `tests/integration/test_graph_writer.py` — new
- `tests/integration/test_schema.py` — the head revision becomes `0006`

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests/ -q
    uv run pace ingest --json      # run 1
    uv run pace ingest --json      # run 2
    docker exec pace-db psql -U postgres -d pace -c \
      "SELECT count(*), min(topo_order), max(topo_order) FROM commits;"
    docker exec pace-db psql -U postgres -d pace -c "SELECT count(*) FROM commit_parents;"
    docker exec pace-db psql -U postgres -d pace -c \
      "SELECT run_id, status, rows_inserted, rows_skipped FROM ingest_runs ORDER BY run_id;"

Expected: `38888 | 1 | 38888`; `38887`; run 2 has `rows_inserted = 0` and `rows_skipped` equal to
run 1's `rows_inserted`.

## Notes

- Every number in checkpoint A is a **first-parent** number, because the walk is first-parent. The
  roadmap's 17,015 for the legacy path counts all parents; first-parent it is 15,844. The write-up
  must say which one it quotes.

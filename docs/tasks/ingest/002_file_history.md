# Task 002: Write file history across renames

**Tracker:** [001_INGEST](../../tracker/001_INGEST.md) · task 5 · **checkpoint A**, slice S2 of 3
**Priority:** P0 · **Status:** ⬜ Not started · **Blocked by:** [001](001_commit_graph.md)
**Measured at:** corpus commit `1e2ad803f74eadb5fb28d4504ca06b008c48b63e`

## User Story

- **Summary:** Give each of the 303 in-scope files one identity across its whole history —
  including the 2025 move of `airflow/` to `airflow-core/src/airflow/` — and record every commit
  that changed it, with the path the file had at that commit.

### Use Case

- **As the** author measuring how much history a current-path view loses
  ([003](003_history_truncation.md))
- **I want** `files` and `file_revisions` populated through the walker's rename resolution
- **so that** "the history of `models/dag.py`" means its whole lineage, and every step of that
  lineage still says what the file was called at the time.

### Background (measured)

`git log --first-parent -- airflow-core/src/airflow/models/dag.py` returns **95** commits; with
`--follow` it returns **611** (both in 1.8 s). The difference is the history before commit
`243fe86d4b` (2025-03-21, "Move airflow sources to airflow-core package (#47798)"), which is also
the first commit that touches any `airflow-core/` path.

The walker already resolves historical paths to today's names (`RenameChains`, `git_walk.py:332`).
Because the walk runs newest → oldest, an alias only ever applies to commits *older* than the
rename that created it, so a path later reused by a different file is not misattributed. Nothing
persists the result yet.

The schema has no column for the path a file had at a given commit: `file_revisions.old_path`
holds only a rename's source (the `file_revisions_rename_has_old_path` CHECK depends on that), so a
2019 change to `airflow/models/dag.py` would be stored with no record of that name.

## Design

| # | Decision | Why | Rejected |
|---|---|---|---|
| 1 | One `files` row per **today's path**, for in-scope paths only (at most 303) | A file's identity is its lineage, named by where it lives now. `scope.txt` lists paths that exist at HEAD, so every row is a live file | One row per historical path (the 2025 move would split every file in two) |
| 2 | One `file_revisions` row per (file, commit) that changed it, written in the same savepoint as its commit | The commit row exists before its revisions, so nothing waits; and a commit is stored with all of its revisions or none of them | A second pass for revisions (after a crash a commit could exist without them) |
| 3 | Migration `0006` adds `file_revisions.path_at_commit text NOT NULL` | Task 003's built-in check and `pace history` both need the name each revision had | Overloading `old_path` (it means "rename source", and its CHECK depends on that) |
| 4 | For a deletion, `blob_sha` is the blob **before** the change | It is the last content the file had; git's all-zero id is a placeholder, not content | Storing the zero id (it satisfies the `git_sha` domain while meaning nothing) |
| 5 | If two changes in one commit resolve to the same file, keep the one whose path is today's path (else the first in git's order) and count the other in `collapsed_changes` | `UNIQUE (file_id, commit_sha)` allows one revision per file per commit; a fixed rule keeps re-runs identical, and the count keeps it visible | Letting the unique index reject the commit (loses the whole commit) |
| 6 | Existing `files` rows are loaded once per run; new ones go in with `ON CONFLICT (repo_id, path) DO NOTHING RETURNING file_id` | At most 303 rows fit in a dict; idempotent, and the counts stay exact | `DO UPDATE … RETURNING` on every change (`rowcount` is 1 either way, so inserted and existing cannot be told apart) |
| 7 | After the walk, one `UPDATE` sets each file's `first_seen_commit` / `last_seen_commit`, touching only rows whose values differ | Run 2 then reports `rows_updated = 0`, which the idempotency check needs | Updating unconditionally (every re-run would report 303 updates) |

`change_type` maps git's status letters: `A` added, `M` modified, `D` deleted, `R` renamed (with
`old_path` and `similarity`), `C` copied (with `old_path` and `similarity`), `T` type_changed. Any
other letter raises, which dead-letters the whole commit (decision 2).

Revisions are written for commits that are already present, so task 002 can run over a database
that task 001 populated: the commit insert is skipped, its revisions are new.

## Acceptance Criteria

- **Scenario:** every in-scope file has an identity and a history
  - **Given:** a full ingest at the pinned commit
  - **When:** it completes
  - **Then:** `files` has exactly one row for each of the 303 paths in `scope.txt`, each has at
    least one revision, and no file's newest revision is a deletion (they all exist at HEAD). The
    run asserts all three; it does not leave them to a reader.

- **Scenario:** history crosses the 2025 move
  - **Given:** `airflow-core/src/airflow/models/dag.py`
  - **When:** its revisions are listed by `commit_topo_order`
  - **Then:** the revision at `243fe86d4b` is `renamed` with `old_path = 'airflow/models/dag.py'`,
    and every older revision has a `path_at_commit` under `airflow/`.

- **Scenario:** a second run writes and updates nothing
  - **Given:** the state after a full ingest
  - **When:** `pace ingest` runs again
  - **Then:** `files` and `file_revisions` report 0 inserted and 0 updated, with every row counted
    as already present.

- **Scenario:** collapsed changes are visible
  - **Given:** a commit with two changes that resolve to the same file
  - **When:** it is written
  - **Then:** one revision is stored by the fixed rule, and `collapsed_changes` counts the other.

## Files to Modify

- `provenance/ingest/graph_writer.py` — `files`, `file_revisions`, first/last-seen update, the
  three invariants
- `migrations/versions/0006_history_columns.py` — `path_at_commit` (the same revision as task 001)
- `provenance/graph/tables.py` — any constants task 001 did not add
- `tests/integration/test_graph_writer.py` — a fixture repo with a rename chain, a delete and
  re-add, and a merge; `tests/unit/` for the collapse rule

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests/ -q
    uv run pace ingest --json      # adds files and revisions
    uv run pace ingest --json      # 0 inserted, 0 updated
    docker exec pace-db psql -U postgres -d pace -c "SELECT count(*) FROM files;"
    docker exec pace-db psql -U postgres -d pace -c \
      "SELECT r.change_type, r.old_path, r.path_at_commit, left(r.commit_sha, 10)
         FROM file_revisions r JOIN files f USING (file_id)
        WHERE f.path = 'airflow-core/src/airflow/models/dag.py'
        ORDER BY r.commit_topo_order DESC LIMIT 100;"

Expected: `303`; the dag.py listing turns from `airflow-core/…` to `airflow/…` at the `renamed` row
for `243fe86d4b`.

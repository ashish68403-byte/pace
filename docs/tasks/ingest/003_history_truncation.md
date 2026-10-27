# Task 003: Measure how much history a current-path view loses

**Tracker:** [001_INGEST](../../tracker/001_INGEST.md) · task 6 · **checkpoint A**, slice S3 of 3
**Priority:** P0 · **Status:** ✅ Done (2026-10-27)
**Measured at:** corpus commit `1e2ad803f74eadb5fb28d4504ca06b008c48b63e`

## User Story

- **Summary:** Checkpoint A's headline number. For every in-scope file, compare the history you get
  by looking the file up under today's path with the history the graph holds, check the graph
  against git's own `--follow`, and commit the result together with the commands that produced it.

### Use Case

- **As the** author defending checkpoint A
- **I want** one command that reproduces every number in the measurement
- **so that** "a current-path view hides X% of this code's history" is a census over all 303 files,
  checked two independent ways, rather than one striking example.

### Background (measured)

`models/dag.py`: 95 commits by today's path, 611 with `git log --follow`. ROADMAP Trap 2 quotes the
same pair; review finding 18 notes that several Trap 2 numbers have no committed derivation. This
task gives the file-level ones one. No `airflow-core/` path exists before commit `243fe86d4b`
(2025-03-21), so a current-path view of any in-scope file starts on that date.

## Design

Three sets of commits per in-scope file `f`, all first-parent:

| Set | Source | Meaning |
|---|---|---|
| `P(f)` current path | `git log --first-parent --format=%H -- <today's path>` | What a user gets with no rename handling |
| `R(f)` rename-aware | the revisions of `f` in the graph | What PACE knows |
| `F(f)` git follow | `git log --first-parent --follow --format=%H -- <today's path>` | git's own rename-following, one file at a time; an independent check on `R` |

Reported, per file and in total:

- **Hidden share**, `|R \ P| / |R|` — the fraction of a file's history the current-path view never
  shows. Per-file median and interquartile range, the share of files above 50%, and the pooled share
  over all (file, commit) pairs. Also the number of distinct commits that are hidden for every
  in-scope file they touch.
- **Hidden with a PR reference** — how many commits in `R \ P` have a subject carrying `(#1234)`:
  history with a review discussion behind it that the current-path view never reaches. First in the
  cut order if time runs short.
- **Agreement with `--follow`** — precision `|R ∩ F| / |R|`, recall `|R ∩ F| / |F|`, F1, and up to
  five example commits for each file where they disagree. Called *agreement*, not accuracy:
  `--follow` is a similarity heuristic too, so the write-up shows the disagreements instead of
  declaring a winner.

**Built-in correctness check.** `P(f)` from git must equal the graph's own current-path set: the
revisions of `f` whose diff touched today's path. These are two independent computations — git's
pathspec, and the walker's rename resolution — and a mismatch means one of them is wrong, or that
the literal path string was genuinely reused by two unrelated files over time (a file renamed away,
with something else later added back at the same path). The second case is real and measured on
this corpus — see Notes — so a mismatch is explained before it is allowed to pass: a commit left
over from `P \ graph_current` is accepted only when it predates the file's own oldest stored
revision *and* that revision is an addition, not a rename-in (meaning our own walker asserts no
further lineage exists). Anything else fails the command and lists the files.

**No intervals, on purpose.** The 303 files are the whole population, not a sample, so the
distribution has no sampling error. The uncertainty lives in the rename heuristics, and the
`--follow` agreement is what quantifies it. The write-up says so.

**It refuses to measure nothing.** With no `files` rows for the repository, the command exits 2 with
"run `pace ingest` first" and writes nothing, rather than printing zeros — the rule
`scripts/leakage_probe.py` already applies with INCONCLUSIVE.

Two commands:

- `pace measure truncation` writes `results/history_truncation.json`: corpus commit, scope version,
  `git --version`, the exact commands, per-file rows and aggregates, with sorted keys and no
  timestamps, so re-running it produces a byte-identical file. `results/` is new and committed, with
  a short README saying these are the numbers the write-up quotes. `artifacts/` stays gitignored as
  CI scratch.
- `pace history <path>` prints one file's history from the graph — date, short SHA, the path at that
  commit, subject — marks the commits a current-path view misses, and ends with the counts. This is
  checkpoint A's demo.

## Acceptance Criteria

- **Scenario:** the measurement covers every file
  - **Given:** a full ingest
  - **When:** `uv run pace measure truncation` runs
  - **Then:** `results/history_truncation.json` has one row for each of the 303 scope paths plus the
    aggregates above, and the built-in check passed for all 303.

- **Scenario:** it reproduces
  - **Given:** the same corpus commit and graph
  - **When:** the command runs twice
  - **Then:** the two output files are byte-identical.

- **Scenario:** the worked example matches git
  - **Given:** `airflow-core/src/airflow/models/dag.py`
  - **When:** its row is read
  - **Then:** `n_current_path = 95` and `n_follow = 611`, as the commands above return, alongside
    the graph's own count and its agreement with `--follow`.

- **Scenario:** an empty graph is refused
  - **Given:** no `files` rows for the repository
  - **When:** the command runs
  - **Then:** it exits 2 and writes nothing.

- **Scenario:** the demo
  - **Given:** a full ingest
  - **When:** `uv run pace history airflow-core/src/airflow/models/dag.py` runs
  - **Then:** it lists the file's rename-aware history with the path at each commit, marks the
    commits a current-path view misses, and ends with both counts.

## Files to Modify

- `provenance/analysis/__init__.py`, `provenance/analysis/truncation.py`,
  `provenance/analysis/history.py` — new
- `provenance/cli.py` — `pace measure truncation`, `pace history`
- `results/README.md`, `results/history_truncation.json` — new, committed
- `tests/unit/test_truncation_metrics.py`, `tests/integration/test_truncation.py` — new

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests/ -q
    uv run pace measure truncation
    uv run pace measure truncation && git diff --exit-code results/   # byte-identical
    uv run pace history airflow-core/src/airflow/models/dag.py | tail -5

## Notes

- **2026-10-27 — the first real run found a defect, in the check, not the walker.** Run 1 against
  the full corpus failed the built-in check on 3 of 303 files:
  `notifications/__init__.py`, `stats.py`, `observability/stats.py`. Root-caused with
  `superpowers:systematic-debugging` by reading the raw `--raw` diff lines (the exact flags
  `raw_log_stream` uses) for every mismatched commit, not by guessing. All three share one shape:
  a file is renamed away (`stats.py` → `observability/stats.py` at `337aee896a`), and a *different,
  unrelated* blob is later added back at the old literal path (a backcompat shim). git's naive
  `git log -- <path>` counts both lineages, because it only asks "did the blob at this exact string
  change", oblivious to where the content came from or went. Our rename-aware graph correctly keeps
  them as two separate files — `notifications/__init__.py` today has exactly 2 revisions (its own
  backcompat-shim history), not the 4 that the naive path log reports. So the graph was right and
  the acceptance criterion's premise ("no `airflow-core/` path predates the move") was false: a path
  can be reused by an unrelated later file even though every reuse here postdates the move.
  **Fix, in the check, not the writer:** a reproducible unit test
  (`tests/integration/conftest.py::reused_path_repo`, a 4-commit fixture: add → rename-away → add
  again, unrelated content → modify) was written and watched fail before `_explain_path_reuse` was
  added. A mismatched commit is explained, never silently dropped, when it predates the file's own
  oldest stored revision *and* that revision is an `added` change — meaning our own walker already
  asserts no further lineage exists, so an older naive-log hit is provably someone else's history
  reusing the same string. If the genesis is instead a `renamed` change, no auto-explanation is
  given, because an unexplained gap there could be a real missing revision. Every explained commit
  is still reported (`current_path_mismatch_explained`), never hidden.
- **2026-10-27 — the real numbers.** Full corpus, both runs byte-identical
  (`sha256 b49198dc…`), 303/303 files pass. A current-path view hides **75.6%** of all (file,
  commit) history (median per file 70.0%, IQR 25.0–83.3%; 194 of 303 files hide more than half).
  5,243 of 6,810 commits touching in-scope files are hidden from *every* current-path lookup that
  could show them, and 82.4% of that hidden history references a PR. Agreement with `git --follow`:
  precision 0.9912, recall 0.8657, 191 files agree exactly. `models/dag.py`: 95 commits by current
  path, 611 rename-aware (hidden share 84.5%), 611 via `--follow` (precision and recall both 1.0).
  Commands: `uv run pace measure truncation`, run twice, `sha256sum results/history_truncation.json`.

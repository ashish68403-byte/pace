# Task 001: Blame and `git log -L` for the questions' functions — arms A and L, stored

**Tracker:** [003_SYMBOLS_BLAME](../../tracker/003_SYMBOLS_BLAME.md) rows 1 and 2 · **checkpoint C**,
slice C2 of four
**Priority:** P0 · **Status:** ⬜ Not started
**Decision records:** [ADR-0005](../../ADR-0005-lineage-vs-blame.md) §2 and §6, and its addendum of
2026-11-09; [`PREREGISTRATION.md`](../../PREREGISTRATION.md)'s amendment of the same date — both
in this spec's commit
**Depends on:** [function-identity/001](../function-identity/001_preregistration.md) (the measure,
and the tests' own database); checkpoint A's `commits.is_mechanical`

## User Story

- **Summary:** For each of the 1,777 functions that the 4,206 retained dev questions name, store
  who owns every line at the pinned commit, both by plain blame and with mechanical commits
  ignored. Also store every commit `git log -L` lists for each function, so slice C4 reads arms A
  and L from the database. Report what was stored, trap 3 re-measured on these functions, and
  whether either tool stops at the 2025 directory move.

### Use Case

- **As the** author defending checkpoint C
- **I want** the blame arm built exactly as ADR-0005 fixes it, with its mechanical-commit filter
  measured rather than assumed
- **so that** "your blame baseline is a straw man" and "`git log -L` would have done this" are
  answered by stored, reproducible data.

### Background (measured 2026-11-09)

- **The functions.** The 4,206 retained dev records name 1,777 distinct functions in 215 files.
  All 215 files are in `files`, every record has exactly one gold commit, and all of them are
  pinned at `1e2ad803f7`.
- **Spans.**
  - 1,592 functions are one chunk.
  - 165 are split into fragments, whose spans cover body statements only: not the decorators,
    the signature or the docstring.
  - 20 have several parts under one name, such as a property's getter and setter.
- **Side branches.** The corpus has 40,995 commits reachable from the pinned one. Checkpoint A
  stored the 38,888 on the first-parent line, and `blame_ranges.orig_commit_sha` must be one of
  them. Plain `git blame` can name any of the other 2,107, and no gold commit is one of them.
- **The mechanical list.** 2,223 commits are mechanical (`commits.is_mechanical`), Airflow's own
  `.git-blame-ignore-revs` among them. On six sampled functions, plain blame put up to 14% of a
  function's lines on a mechanical commit; with the list ignored, none.
- **git 2.43.0, timed on six functions.**
  - `git blame` with this slice's flags takes 0.2 to 2.6 s per function; `git log -L` about
    0.8 s.
  - One blame call with one `-L` per function gives every line the same owner as one call per
    function: on the three files with the most functions (4,573 lines), none differ.
- **The 2025 move.** `243fe86d4b` (2025-03-21) moved `airflow/` to `airflow-core/src/airflow/`.
  On one sampled function in `models/xcom.py`, both tools followed it:
  - `git log -L` listed 20 commits from before the move;
  - blame put all six owners at the old path.

  ADR-0005 left this question to C2.
- **A line an ignored commit added.** `git blame --ignore-revs-file` re-attributes a line the
  ignored commit changed. A line that commit added stays on it; `blame.markUnblamableLines`
  marks it with `*`, and porcelain output gives no sign of it.
- **Tables.** `blame_ranges` (migration 0002) is empty. `edge_symbol_commit` needs C3's lineage
  ids, and its `source` check admits no `git log -L`.

## Design

### Units

| Unit | Kind | Responsibility |
|---|---|---|
| `provenance/parse/chunker.py` | pure | Every chunk carries its definition's span, so a fragment knows its whole function's lines. |
| `provenance/parse/symbols.py` | pure | `symbol_spans(text, path)`: each function's full span or spans, named as `symbols()` names them. |
| `provenance/ingest/blame.py` (new) | pure, then git, then the database | The porcelain and `git log -L` parsers and range merging; repo-agnostic `blame_lines` and `line_log`; the build. |
| `migrations/versions/0010_line_log.py` (new) | schema | `line_log_commits`. |
| `provenance/analysis/blame_census.py` (new) | read-only | `results/blame_arms.json`. |
| `provenance/cli.py` | — | `pace blame build`, `pace measure blame`. |

### Spans

A function's span runs from its first decorator to its last line. The rules:
- a name with several parts (a getter and a setter) has one span per part;
- a split function has one span, its whole definition;
- `symbol_spans` and `symbols()` name the same functions.

### Blame, twice per file

The build covers the retained dev records. It blames each of the 215 files, in path order, twice:

- **Plain:** `git blame --porcelain -w -M --first-parent -L <s>,<e> [-L …] <pinned> -- <path>`,
  with one `-L` per span of the file's needed functions.
- **Filtered:** the same command, plus `--ignore-revs-file <list>`. The list holds every
  `commits.is_mechanical` SHA, sorted, and is read from the database at run time.

**Rows.** One `blame_ranges` row covers the longest run of consecutive lines that share all of:
the plain owner, its original path, consecutive original lines, and the filtered owner. Its
columns hold:
- `orig_commit_sha`, `orig_path`, `orig_line_start`: plain blame's owner and where it wrote the
  line;
- `effective_commit_sha`: the filtered owner, or NULL when that owner is still a mechanical
  commit;
- `snapshot_sha`: the pinned commit;
- `chunk_id`: NULL;
- `blame_algo`: the git version, the flags, and the list's size and sha256.

**One transaction per file.** A file is skipped when all its needed lines have rows made with
the same `blame_algo`, so the build resumes.

### `git log -L`, once per function

- **The command:** `git log --first-parent -L <s>,<e>:<path> [-L …] --format=%H -s <pinned>`,
  with one `-L` per span.
- **Storage:** every listed commit goes into `line_log_commits` (repository, file, snapshot,
  qualified name, commit, `algo`), mechanical ones included. Arm L drops those when C4 reads the
  table (ADR-0005 §2).
- **Resuming:** a function with rows made with the same `algo` is skipped.

### What C4 reads

- **Arm A:** a function's distinct non-NULL `effective_commit_sha` values over its spans.
- **Blame, unfiltered:** its distinct `orig_commit_sha` values. This is a labelled row beside the
  arms, not pre-registered (tracker 003 row 2).
- **Arm L:** its `line_log_commits`, with mechanical commits removed.

### The report

`pace measure blame` writes `results/blame_arms.json`. Two runs give the same bytes; run times
go to the log, not the file. It holds:
- **inputs:** the golden set's sha256, the pinned commit, the git version, both commands, and the
  list's size and sha256; the counts of questions, functions, files, lines and rows;
- **trap 3 on these functions:** the share of lines whose plain owner is mechanical, and the
  share stored with a NULL filtered owner;
- **per function,** for arm A, unfiltered blame and arm L: the median, 90th-percentile and
  largest commit counts, and how many functions have an empty arm A;
- **the move:** first, the functions in files `243fe86d4b` renamed whose qualified name existed
  in the file just before the move. Of those, how many have a `git log -L` commit from before the
  move, and how many have a blame owner at the old path.

It holds no gold, no recall and no comparison: those belong to C4, after the pre-registration.

### Refusals

The build stops, having written nothing for the file at hand, when:
- a question's function is not found at the pinned commit, a path is not in `files`, or a
  record is pinned to another commit;
- git fails: its message is shown, and a rerun resumes;
- the clone is shallow, or the mechanical list is empty;
- stored rows were made with a different `blame_algo` or `algo`. `--rebuild` first deletes this
  repository's rows at the pinned commit.

`pace measure blame` refuses while any function lacks stored rows.

### Not in this slice

- **Lineage and its oracle check** (C3), which reuses `line_log` on CodeShovel's projects.
- **The comparison and `pace eval provenance`** (C4).
- **HEAD code chunks** (D).
- **The test split's functions:** they are built at that split's final opening, when Claims 1
  and 2 are re-measured on it.

## Acceptance Criteria

- **Scenario:** the amendments precede every blame number
  - **Then:** ADR-0005's addendum and PREREGISTRATION.md's amendment are in this spec's commit,
    before any commit that adds blame code.

- **Scenario:** spans
  - **Then:**
    - a decorated function's span starts at its first decorator;
    - a getter and setter give two spans under one name;
    - a split function gives one span that holds its signature and docstring;
    - `symbol_spans` and `symbols()` return the same names.

- **Scenario:** a side branch's change
  - **Given:** a function's line changed on a side branch that a merge commit brought in
  - **Then:** both owners and `git log -L` name the merge commit, never the side commit.

- **Scenario:** a mechanical commit
  - **Given:** a commit marked mechanical that changed one line of a function and added another
  - **Then:** the changed line's plain owner is that commit, and its filtered owner the commit
    that wrote the line before it; the added line's filtered owner is NULL.

- **Scenario:** a directory move
  - **Then:** blame keeps the old path for lines from before the move, and `git log -L` lists
    commits from before it.

- **Scenario:** resume and determinism
  - **Then:**
    - a second build inserts no row;
    - one multi-range blame gives each line the owner that per-function blame gives;
    - two reports are byte-identical.

- **Scenario:** refusals
  - **Then:** a missing function, an empty mechanical list, and rows from another `blame_algo`
    each stop the build, with nothing written for that file.

## Files to Modify

- In the spec commit: `docs/ADR-0005-lineage-vs-blame.md` (addendum), `docs/PREREGISTRATION.md`
  (amendment), `docs/tracker/003_SYMBOLS_BLAME.md` (header)
- `provenance/parse/chunker.py`, `provenance/parse/symbols.py`
- New: `provenance/ingest/blame.py`, `migrations/versions/0010_line_log.py`,
  `provenance/analysis/blame_census.py`
- `provenance/cli.py`
- Tests: unit rules for spans, porcelain, ranges and `git log -L`; an integration test on a new
  fixture repo
- `results/blame_arms.json` (new, committed)
- Later: trackers 003 and 004, and ROADMAP §4 trap 3's citations (tracker 003 row 3)

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    uv run pace blame build            # resumable
    uv run pace measure blame          # twice: the same bytes

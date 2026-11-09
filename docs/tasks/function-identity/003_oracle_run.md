# Task 003: The oracle run — claim 3 on CodeShovel's Python oracle, once, on the frozen engine

**Tracker:** [004_FUNCTION_IDENTITY](../../tracker/004_FUNCTION_IDENTITY.md), verification step 1
(the oracle's number) · **checkpoint C**, slice C3b
**Priority:** P0 · **Status:** ✅ Done (2026-11-09)
**Decision records:** [ADR-0005](../../ADR-0005-lineage-vs-blame.md) §7–§8 and its addendum of
2026-11-09 on the oracle run; [`PREREGISTRATION.md`](../../PREREGISTRATION.md) claim 3 and its
amendment of the same date — both in this spec's commit
**Depends on:** [function-identity/002](002_lineage_engine.md) (the frozen engine,
`lineage-mvp-1`); [symbols-blame/001](../symbols-blame/001_blame_arms.md) (`line_log`, arm L's
command)

## User Story

- **Summary:** Run the frozen lineage engine once on CodeShovel's 40 Python oracle functions,
  beside `git log -L`. Score both against the oracle's labels by rules fixed before the run, and
  decide claim 3 in the pre-registered words.

### Use Case

- **As the** author defending checkpoint C's identity claim
- **I want** one run of an engine that never saw an oracle function, scored by rules committed
  before it
- **so that** claim 3's verdict, whichever way it goes, is a test and not a fit.

### Background (measured 2026-11-09; structure only, no arm has run on the oracle)

- **The oracle.** CodeShovel `0d29f498cc` (2024-01-31, MIT), `src/test/resources/oracles/python/`:
  40 functions, four from each of ten projects, with 327 labelled commits. Each file gives the
  repository, the file's path at a start commit, the function's name and its `def` line.
- **The clones.** `~/corpus/oracle/<project>.git`, bare, 2.3 GB, read as data. Every start
  commit and label is present, and every label is an ancestor of its start commit. 61 labels, in
  22 functions, are off the start commit's first-parent line. There are 25 distinct start commits.
- **The functions.** 24 methods, 13 top-level functions and 3 functions nested in another; 2 are
  a property's getter or setter. The symbol layer names 37, all but the 3 nested ones.
- **The 16.** 16 functions hold a `Yrename`, `Yfilerename` or `Ymovefromfile` label, counting
  those inside a `Ymultichange`: ADR-0005's 16.
- **The size.** The ten projects' first-parent histories hold about 192,000 Python file revisions
  (django 62,858, pandas 47,463, zulip 32,737). Walking all of them would take about 1.3 hours at
  Airflow's rate.
- **The engine's inputs.** `walk_file` and `link_moves` are pure: they take file revisions, which
  on Airflow come from checkpoint A's `file_revisions`. The ten projects were never ingested.
  Ingesting them would store their commit authors' names, and DATA-PROTECTION.md limits PACE to
  one repository, apache/airflow.
- **What already exists.** `preregistration.identity(gain, lo)` (C1) returns "holds" or claim
  3's failure sentence. `metrics.paired_bootstrap` gives the paired interval (10,000 resamples,
  seed 20250131). `blame.line_log` runs arm L's command.

## Design

### Units

| Unit | Kind | Responsibility |
|---|---|---|
| `provenance/lineage/history.py` (new) | git | A repository's rename-aware, first-parent file histories in memory, from one start commit: checkpoint A's `raw_log_stream`, `RenameChains`, `one_change_per_file` and `revision_row`, with `walk_history`'s topo order. Also the Python files each commit touched. |
| `provenance/lineage/scope.py` (new) | pure | Following a chain: walk one file, link, then add every Python file touched at a commit where a piece of the tracked identity begins, until nothing is added. Calls `symbols_at`, `walk_file` and `link_moves` unchanged. |
| `provenance/eval/oracle.py` (new) | read-only | Read the oracle files as data; resolve each function; project the labels; run both arms; score; decide claim 3; `pace eval oracle` → `results/oracle.json`. |
| `provenance/cli.py` | | `pace eval oracle`. |

The engine is not edited in this slice: `provenance/lineage/match.py`, `link.py`,
`normalize.identity_hashes` and `build.symbols_at` stay as C3a froze them.

### The files walked

For each function, from its start commit:
1. its own file, rename-aware, as checkpoint A follows a file;
2. after linking, every Python file (a path ending in `.py`) touched by a commit where a fragment
   of the tracked identity begins (its birth, or its arrival in a file);
3. step 2 again, until it adds nothing.

The cross-file rule links a birth and a death at one commit, and both lie in files that commit
touched. So this finds every candidate a walk over the whole project would, and the tracked
identity is the same, in minutes instead of about 1.3 hours. Nothing is written to the database.

### Resolving a function

At its start commit, the oracle's `def` line (`functionStartLine`) gives:
- **`git log -L`'s span:** that definition's lines, decorators included;
- **lineage's identity:** the one alive at the start commit whose span holds the line. That is the
  function itself for 35 functions, the enclosing function for the 3 nested ones, and for the 2
  property parts the property, both parts under one name as the symbol layer names it.

Any bias from lineage's coarser unit falls on lineage.

### The three commit sets

- **The oracle's:** the labels. A label off the start commit's first-parent line is replaced by
  the oldest commit on that line that has it as an ancestor: the merge that brought it in, where
  a first-parent walk first sees the change. Duplicates collapse, so the 40 sets hold 310 commits.
- **Lineage's:** every version commit of the identity (birth, token stream, name, path), up to
  the start commit.
- **`git log -L`'s:** `git log --first-parent -L <s>,<e>:<path> --format=%H -s <start>`, through
  `blame.line_log`.

No mechanical filter applies: these projects have no mechanical list, and CodeShovel labels a
reformat that changes tokens.

### The score

- **Per function and arm:** precision, recall and F1 against the oracle's set.
- **Claim 3:** the mean F1 of lineage minus that of `git log -L` over the 40 functions, with
  `paired_bootstrap`'s interval (unstratified: the functions have no classes), decided by
  `preregistration.identity`.
- **Reported apart, deciding nothing:** the 16 rename-or-move functions; the 5 nested or property
  functions; mean precision and recall per arm and group.

### The report

`pace eval oracle` writes `results/oracle.json`. Two runs write the same bytes. It holds:
- **inputs:** this repository's commit, `PREREGISTRATION.md`'s last commit, CodeShovel's commit
  and a SHA-256 over its 40 oracle files, git's version, and the detector `lineage-mvp-1`;
- **per function:** its project, kind and groups, its start commit, the three commit sets, how
  many files were walked, and each arm's precision, recall and F1;
- **per arm and group:** the mean precision, recall and F1;
- **claim 3:** the difference, its interval, n, and the verdict.

### The gate

`pace eval oracle` writes nothing unless:
- no tracked file is modified, so the recorded commit is the code that ran, and
  `docs/PREREGISTRATION.md` is committed;
- the oracle directory holds 40 Python oracle files, and CodeShovel's clone is at `0d29f498cc`;
- every start commit and label is in its clone, and every label is an ancestor of its start
  commit;
- every function resolves: a definition at its `def` line, and an identity holding that line
  alive at the start commit.

### The run, once

- **When:** on the slice's branch, after its code and tests are committed, so the recorded
  commit holds every line that ran.
- **Once:** the run is repeated once, unchanged, to show the same bytes, and then committed.
- **After it:** the engine is never changed for the oracle. A defect found after the run in this
  slice's own code (the reader, the histories, the chain loop, the projection or the score) is
  fixed and the run repeated, and both runs are reported, the first kept in git history.

### Not in this slice

- CodeShovel itself as a third arm.
- Extract and inline edges, nested functions as identities, and similarity matching (the rename
  made with an edit, ADR-0005's Addenda).
- `pace eval provenance` and claims 1 and 2 (C4).

## Acceptance Criteria

- **Scenario:** the rules precede the run
  - **Then:** ADR-0005's addendum and PREREGISTRATION.md's amendment are in this spec's commit,
    before any commit that adds oracle code, and `results/oracle.json` records a later commit.

- **Scenario:** in-memory histories
  - **Then:** on a fixture repository, `history.py`'s revisions (commit, topo order, path at
    commit, change type, blob) equal the `file_revisions` that `pace ingest` writes for it.

- **Scenario:** following the chain
  - **Then:** a function moved in from a file outside its own file's history keeps its earlier
    versions, and its identity equals the one a walk over every Python file in the fixture gives.

- **Scenario:** projection
  - **Then:** a label on a merged side branch counts at its merge, a first-parent label is
    unchanged, and two labels on one merge count once.

- **Scenario:** units
  - **Then:** a nested function's lineage set is its enclosing function's, while `git log -L`
    gets its own lines; a property getter's lineage set is the property's.

- **Scenario:** the score
  - **Then:** on the fixture, each arm's commit sets and scores are as expected, and the claim-3
    verdict is `preregistration.identity`'s words.

- **Scenario:** the gate
  - **Then:** a modified tracked file, an oracle directory at another CodeShovel commit, a missing
    label, a label that is not an ancestor of its start commit, and a function that does not
    resolve each write nothing.

- **Scenario:** determinism
  - **Then:** two runs write the same bytes.

## Files to Modify

- In the spec commit: `docs/ADR-0005-lineage-vs-blame.md` (addendum), `docs/PREREGISTRATION.md`
  (amendment), `docs/tracker/004_FUNCTION_IDENTITY.md` (header)
- New: `provenance/lineage/history.py`, `provenance/lineage/scope.py`, `provenance/eval/oracle.py`
- `provenance/cli.py`
- Tests: unit rules for the projection, the grouping, the chain loop and the gate; an integration
  fixture repository with CodeShovel-format oracle files; the history-equals-ingest test in
  `pace_test`
- `results/oracle.json` (new, committed)
- Later: tracker 004, the ROADMAP's status, `results/README.md`, `docs/RUNBOOK.md`

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    uv run pace eval oracle --codeshovel ~/corpus/codeshovel --clones ~/corpus/oracle
    # once more, unchanged: the same bytes

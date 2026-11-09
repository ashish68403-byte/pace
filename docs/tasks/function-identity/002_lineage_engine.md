# Task 002: The lineage engine — Airflow's function identities, stored and checked against `git log -L`

**Tracker:** [004_FUNCTION_IDENTITY](../../tracker/004_FUNCTION_IDENTITY.md) rows 3 and 4 ·
**checkpoint C**, slice C3a (C3 is split: C3a builds and checks the engine on Airflow, C3b runs it
once on CodeShovel's oracle)
**Priority:** P0 · **Status:** ✅ Done (2026-11-09)
**Decision records:** [ADR-0005](../../ADR-0005-lineage-vs-blame.md) §7–§8 and its Errata of
2026-11-09 (change detection, name-blind hashes); [`PREREGISTRATION.md`](../../PREREGISTRATION.md)'s
amendment of the same date — both in this spec's commit
**Depends on:** checkpoint A's `file_revisions`; [symbols-blame/001](../symbols-blame/001_blame_arms.md)
(arm L's stored `git log -L`, which the agreement check reads)

## User Story

- **Summary:** Walk the history of Airflow's 303 in-scope files and give every function and method
  one identity that survives renames, file renames and one kind of cross-file move. Store each
  identity and every version in `symbol_lineage` and `symbol_versions`, so slice C4 reads arm C
  from the database. Report what was found, Airflow's identity-break rate, and the pre-registered
  agreement check with `git log -L`.

### Use Case

- **As the** author defending checkpoint C's contribution
- **I want** the lineage layer built once, deterministically, and checked against `git log -L`
  before CodeShovel's oracle ever sees it
- **so that** claim 3 tests an engine frozen in advance, and claims 1–2 read a lineage that is
  stored, reproducible and audited.

### Background (measured 2026-11-09)

- **The work.** Checkpoint A's `file_revisions` holds 15,421 revisions of the 303 files, across
  6,810 commits, each with its path at that commit and its blob. 2,516 commits touch two or more
  of the files, which is where a cross-file move can be seen.
- **The tables.** Migration 0002's `symbol_lineage` (one row per identity) and `symbol_versions`
  (one row per identity and commit) are empty. `symbol_versions.match_stage` admits only `h0`,
  `h1`, `h2`, `h3`, `rename_detect` and `manual`, which assumed matching by hash alone.
- **The hashes.** `normalize.hash_ladder` computes h0–h3 over a definition's text. h1 and h3 both
  include the function's own name: h3 renames locals and nested definitions, not the function
  itself. A pure rename therefore changes both, so matching needs name-blind variants.
- **What CodeShovel counts as a change.** Its Python body is the ANTLR text of the function's
  suite (`PythonFunction.getInitialBody`). Its pinned grammar (antlr/grammars-v4 `8803cbe8`,
  `python/PythonLexer.g4`) sends `COMMENT`, `WS` and `NEWLINE` to the hidden channel, and string
  literals, docstrings among them, are ordinary tokens. So a body change is a change in the token
  stream. CodeShovel also labels a commit that only renames the function's file (`Yfilerename`).
  ADR-0005 §7 said otherwise; its Errata correct it.
- **The oracle, by structure only.** The ten projects were cloned into `~/corpus/oracle/` (bare,
  2.3 GB) and their 40 oracle functions were classified. No identity tracking ran on them.
  - 24 are methods, 13 are top-level functions, and 3 are nested in another function;
  - 2 are a property's getter or setter;
  - 37 are named by our symbol layer (all but the 3 nested ones);
  - their 327 labelled commits are all in the clones, and 61 of them (in 22 functions) are off
    the first-parent line.

  C3b decides how to score these; C3a keeps the symbol layer's unit (top-level functions and
  methods, a property's parts under one name).
- **Arm L's facts from C2.** Of the 1,777 question functions, 1,061 existed before the 2025 move
  in files it renamed. `git log -L` reaches past the move for 1,049 of them and not for 12.

## Design

### Units

| Unit | Kind | Responsibility |
|---|---|---|
| `provenance/parse/normalize.py` | pure | `identity_hashes(source)`: h1 and h3 with the definition's own name replaced by a placeholder. |
| `provenance/lineage/match.py` (new) | pure | Pass 1: one file's revisions into identity fragments. |
| `provenance/lineage/link.py` (new) | pure | Pass 2: births and deaths in different files at one commit into moves; fragments into lineages. |
| `provenance/lineage/build.py` (new) | database, git | Read `file_revisions` and blobs, parse, run both passes, write both tables; `pace lineage build`. |
| `migrations/versions/0011_lineage_stages.py` (new) | schema | `match_stage` admits `birth`, `name`, `h1`, `h3` and `move`. |
| `provenance/analysis/lineage_census.py` (new) | read-only | `pace measure lineage` → `results/lineage.json`. |

### Pass 1, one file at a time

Each file's revisions are read in first-parent order. A merge is its diff against its first parent,
so a side branch's change belongs to its merge. Each revision is compared with the file's last
parsed state:

1. **The same qualified name is the same identity.**
2. **Leftovers** are the names that vanished and the names that appeared in this revision. An equal
   name-blind h1 is a rename with an unchanged body (stage `h1`). Then an equal name-blind h3 is a
   rename with local renames (stage `h3`). A hash shared by more than one leftover, on either side,
   links nothing and is counted as ambiguous.
3. **What is left over** is a birth (an appearance) or a death (a disappearance). A deleted file's
   functions all die at that commit.
4. **A revision the chunker refuses** leaves the file's last state in place; the next parseable
   revision is compared with it. Such revisions are counted.

### Pass 2, across files

A birth in one file and a death in another, at the same commit, with identical name-blind h3 are
one identity that moved (stage `move`, a rename too when the qualified name changed). Each side
must have exactly one candidate; otherwise nothing is linked and the case is counted. Only the 303
files are walked: a function moved in from any other file is a birth.

### When a version is written

A version row is written:
- at birth;
- when the function's token stream changes (the ordinary h1: code and docstrings count, comments
  and layout do not);
- when its qualified name changes;
- when its path changes, by a file rename or a move.

Every version stores the commit, the path and qualified name at that commit, the span (the range
covering every part, for a name with several parts), the ordinary h0–h3 and `ast_norm_version`,
the stage, `match_score` 1.0 (every match is an exact hash equality), and three flags:
- `is_rename`: the qualified name changed;
- `is_move`: the path changed;
- `is_body_change`: the name-blind h1 changed.

Mechanical commits keep their versions; `commits.is_mechanical` lets the arms drop them (ADR-0005
§2). Each identity stores its kind, its birth and death, its version count, `detector_version`
`lineage-mvp-1`, and, when alive at the pinned commit, its current path and qualified name.

### The build

`pace lineage build` rebuilds the repository's lineage from scratch in one transaction. It is
deterministic and takes minutes, so it never resumes. Function texts are hashed once each
(cached by text).

### The report

`pace measure lineage` writes `results/lineage.json`. Two runs write the same bytes, and nothing in
it is scored against gold. It holds:
- **counts:** identities, versions, births, deaths, renames by stage, moves, ambiguous cases, and
  refused revisions;
- **the identity-break rate** (tracker 004 row 3): among functions alive at `1e2ad803f7`, the share
  whose history holds a function rename, a cross-file move, a file rename, or any of them, each with
  a Wilson 95% interval, with and without the 2025 move (`243fe86d4b`);
- **arm C's size** for the 1,777 question functions, mechanical commits removed: median, 90th
  percentile, largest, and how many are empty;
- **the agreement check** (below).

### The agreement check, fixed here before any lineage number

PREREGISTRATION.md's "Checks, not claims":
- **The sample:** 100 of the 1,777 question functions, in two groups of 50.
  - **Moved:** functions that existed before `243fe86d4b` in files it renamed. This takes all 12
    whose `git log -L` does not reach past the move, and 38 of the 1,049 whose does.
  - **Other:** 50 of the remaining 716.
  - **The order:** within each group, functions are sorted by the sha256 of
    `"pace-agreement-v1:" + path + "::" + qualified_name`, and the first ones are taken.
- **The score:** per function, commit-set F1 between its lineage's commits and its stored
  `git log -L` commits, with mechanical commits removed from both, as the arms use them.
- **The verdict:** the mean F1 and its 95% bootstrap interval are compared with `AGREEMENT_MIN_F1`
  (0.90) overall, and reported for the moved-and-reaches, moved-and-does-not-reach and other
  functions apart. A miss is reported, and it qualifies claims 1–3.

### Refusals

- **The build** writes nothing when the repository has no `file_revisions` or the clone is shallow.
- **The report** refuses before a build, after a build by another `detector_version`, and when any
  of the 1,777 question functions has no identity alive at the pinned commit (ADR-0005 §9 needs
  every one).

### Not in this slice

- **The oracle run (C3b):** projecting the 61 side-branch labels onto their merges, scoring the 3
  nested functions and the 2 property parts through their enclosing names, and claim 3's single
  run on the frozen engine.
- **Extract and inline edges** (`lineage_edges` holds them later); nested functions as identities.
- **The comparison and `pace eval provenance`** (C4).

## Acceptance Criteria

- **Scenario:** the amendments precede every lineage number
  - **Then:** ADR-0005's Errata and PREREGISTRATION.md's amendment are in this spec's commit, before
    any commit that adds lineage code.

- **Scenario:** name-blind hashes
  - **Then:** renaming a function keeps its name-blind h1 and h3. Renaming a local keeps h3 but not
    h1. A comment changes neither. A changed docstring changes h1.

- **Scenario:** pass 1
  - **Then:**
    - an edit to a function's comment writes no version, and an edit to its docstring writes one;
    - a rename with an unchanged body is linked at stage `h1`, and one with renamed locals at `h3`;
    - two vanished functions with the same name-blind hash link nothing, and the case is counted;
    - a function present again after a refused revision keeps its identity.

- **Scenario:** pass 2
  - **Then:** a function moved to another in-scope file in one commit keeps its identity with a
    `move` version. A move to a different name is also a rename. Two candidates link nothing.

- **Scenario:** a file rename
  - **Then:** the function keeps its identity and gets a version at the rename, with `is_move` and
    no body change.

- **Scenario:** determinism
  - **Then:** two builds write the same rows, and two reports the same bytes.

- **Scenario:** refusals
  - **Then:** the report refuses before a build, and when a question function has no identity alive
    at the pinned commit.

## Files to Modify

- In the spec commit: `docs/ADR-0005-lineage-vs-blame.md` (Errata), `docs/PREREGISTRATION.md`
  (amendment), `docs/tracker/004_FUNCTION_IDENTITY.md` (header)
- `provenance/parse/normalize.py`
- New: `provenance/lineage/__init__.py`, `match.py`, `link.py`, `build.py`;
  `migrations/versions/0011_lineage_stages.py`; `provenance/analysis/lineage_census.py`
- `provenance/cli.py`, `provenance/graph/tables.py`
- Tests: unit rules for the hashes, pass 1 and pass 2; an integration test on a new fixture repo;
  `tests/integration/test_schema.py`
- `results/lineage.json` (new, committed)
- Later: trackers 004 and 006, the ROADMAP's status, `results/README.md`, `docs/RUNBOOK.md`

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    uv run alembic upgrade head
    uv run pace lineage build
    uv run pace measure lineage        # twice: the same bytes

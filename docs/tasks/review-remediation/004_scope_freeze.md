# Task 004: Freeze the corpus scope, and make the frozen scope the denominator

**Tracker:** [000_REVIEW_REMEDIATION](../../tracker/000_REVIEW_REMEDIATION.md) · finding 4
**Priority:** P0 · **Severity:** critical · **Status:** ⬜ Not started

## User Story

- **Summary:** `pace scope` does not produce the corpus the roadmap describes, and the scope it
  does produce has never been committed. Until both are true, no recall figure in the project has
  a reproducible denominator.

### Use Case

- **As the** author defending this corpus in a viva
- **I want** `pace scope` on a fresh clone to reproduce exactly the 303 files / 81,619 lines §3 claims
- **so that** "show me the 303 files" has an answer, and every recall number is computed over a
  corpus a marker can regenerate.

### Background (measured)

`provenance/ingest/scope.py:57-64` omits `**/api_fastapi/**` from `DEFAULT_EXCLUDE`, and the
explanatory comment at 52-55 omits it too — though line 89 of ROADMAP names it as one of the four
exclusions. Measured against the clone:

| Globs | Files | Lines |
|---|---|---|
| As committed | 480 | 112,259 |
| With `**/api_fastapi/**` appended | **303** | **81,619** |

The second row matches §3 exactly, which proves the roadmap's number was measured with an exclusion
the code lacks. Two compounding defects: `scope.txt` / `scope_stats.json` have never been committed
and are not gitignored, so `load_scope()` raises `FileNotFoundError` and `skeleton.py:263-270` falls
back to "the first `.py` under the corpus" with only a `log.warning` — meaning the 14-span demo
trace was produced over an arbitrary file. And `ScopeDriftError` only fires when a frozen file
already exists, so the first freeze — the one that matters — is unguarded.

### Acceptance Criteria

- **Scenario:** The code reproduces the documented corpus
  - **Given:** a fresh clone and the corpus at the pinned commit
  - **When:** `uv run pace scope` runs
  - **Then:** it reports exactly **303 files / 81,619 lines**, and `api_fastapi` appears in both
    `DEFAULT_EXCLUDE` and the comment above it.

- **Scenario:** The scope is frozen in the repository
  - **Given:** the corrected globs
  - **When:** `pace scope` is run and its outputs committed
  - **Then:** `scope.txt` and `scope_stats.json` are tracked, carry `SCOPE_VERSION`, and
    `load_scope()` succeeds without falling back.

- **Scenario:** The first freeze is guarded
  - **Given:** no `scope.txt` exists yet
  - **When:** `_write_frozen` is asked to freeze a scope whose file count differs from the pinned 303
  - **Then:** it refuses, rather than silently freezing the wrong glob set as `v1`.

- **Scenario:** Drift is caught in CI
  - **Given:** the frozen scope is committed
  - **When:** the `retrieval-gate` job runs `uv run pace scope --check`
  - **Then:** any change to the computed scope fails the build with the diff.

- **Scenario:** The corpus commit is recorded
  - **Given:** a scope report is produced
  - **When:** `ScopeReport` is serialised
  - **Then:** it carries `corpus_commit` from `git rev-parse HEAD`, and ingest asserts the corpus
    HEAD matches the frozen one. (`ci_subset.jsonl` already carries a null `corpus_commit`; this
    populates the same idea one layer up.)

- **Scenario:** The interim state is stated honestly
  - **Given:** this task is not yet complete
  - **When:** a reader reaches ROADMAP lines 22-23 or README line 20
  - **Then:** both say the scope is computed but **not frozen in-repo**, so no recall denominator
    is yet reproducible — rather than "Scope frozen" and "committed and versioned", which are
    currently false.

## Files to Modify

- `provenance/ingest/scope.py` — `DEFAULT_EXCLUDE` (57-64) and its comment (52-55); `ScopeReport`
  (85-95) gains `corpus_commit`; `_write_frozen` gains the first-freeze guard
- `scope.txt`, `scope_stats.json` — new, committed
- `.github/workflows/ci.yml` — add `uv run pace scope --check` to `retrieval-gate`
- `provenance/ingest/skeleton.py` — make the 263-270 fallback an error, not a warning
- `docs/ROADMAP.md` lines 22-23, 89 · `README.md` line 20

## Notes

- Re-derive the "36.5k lines" claim at ROADMAP:89 while here. Measured at the pinned commit,
  `api_fastapi` is **30,640 physical lines / 177 files** — the argument for excluding it survives,
  but the number does not.
- The fallback at `skeleton.py:263-270` is the more dangerous half of this task. A `log.warning` on
  a missing denominator is how a demo trace over an arbitrary file reads as a passing walking
  skeleton. Fail loudly instead — the same rule `leakage_probe.py` already applies to INCONCLUSIVE.

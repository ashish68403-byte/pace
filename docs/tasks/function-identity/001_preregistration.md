# Task 001: Pre-register the experiment, define its measure, and give the tests their own database

**Tracker:** [004_FUNCTION_IDENTITY](../../tracker/004_FUNCTION_IDENTITY.md) row 1 (finding 3) and
[006_EVAL_HARNESS](../../tracker/006_EVAL_HARNESS.md) row 9 · **checkpoint C**, slice C1 of four
**Priority:** P0 · **Status:** ⬜ Not started
**Decision records:** [ADR-0005](../../ADR-0005-lineage-vs-blame.md) and
[`PREREGISTRATION.md`](../../PREREGISTRATION.md), both in this spec's commit;
[ADR-0004](../../ADR-0004-gold-validity-audit.md) §8
**Depends on:** [eval-harness/007](../eval-harness/007_dense_and_fusion.md) (B5's comparison code,
which C4 reuses)

## User Story

- **Summary:** Commit checkpoint C's experiment and its pre-registration before any blame, lineage
  or comparison number exists. Implement provenance-chain recall in `metrics.py`, with the
  equal-size rule fixed in code. Put the pre-registered thresholds and decision rules in code,
  tested against `PREREGISTRATION.md`. Move the test suite off the real database.

### Use Case

- **As the** author defending checkpoint C
- **I want** the experiment's measure, its control and its falsifiers fixed in a commit that
  precedes every result, and the numbers it produces untouched by the test suite
- **so that** "did you decide that before or after you saw the number?" has a one-line answer
  with a commit hash, and no comparison depends on cleaning an index by hand.

### Background (measured 2026-11-08)

- **Finding 3.** No module defines provenance-chain recall, nothing sizes a control arm, and no
  document states what result would make the thesis's claim fail.
- **The tests share the real database.** Integration tests write rows into `pace` and delete them,
  and pg_search counts those rows in BM25's statistics until they are vacuumed and merged. On
  2026-11-08 the index held 71,253 documents and 2,162 deletions for 69,769 live rows, and 12 of
  4,206 BM25 ranks had moved since slice B5a with no code change (tracker 006 row 9). Checkpoint
  C4 reports numbers that must not drift that way.
- **The tests must make their own data.** CI's database starts empty: a B5b test that assumed
  `pace`'s rows failed CI until it was fixed (`1096107`).
- **Alembic, the application and the tests all read `PACE_DATABASE_URL`** (through
  `provenance.config.settings` and `tests/conftest.py`), so one variable decides where everything
  connects.
- **The oracle check is done** (ADR-0005, Context): CodeShovel's Python oracle exists as ROADMAP
  §7 describes, and needs no fallback.

## Design

### Units

| Unit | Kind | Responsibility |
|---|---|---|
| `docs/ADR-0005-lineage-vs-blame.md`, `docs/PREREGISTRATION.md` | — | In this spec's commit, before any code. |
| `provenance/eval/metrics.py` | pure | `chain_hit` and `equal_size_chance`, the per-question values of provenance-chain recall. |
| `provenance/eval/preregistration.py` (new) | pure, then a file | The five threshold constants, their reader from `PREREGISTRATION.md`, and the pre-registered decision rules. |
| `tests/conftest.py` | — | The test database: chosen before anything imports `provenance`, created when missing, never `pace`. |

### Provenance-chain recall

- **`chain_hit(commits, gold) -> float`:** 1.0 when any gold commit is among the arm's commits,
  else 0.0. Arms A, C and L use it.
- **`equal_size_chance(blame_count, lineage, gold) -> float`:** the exact probability that a
  uniformly random subset of `lineage`, as large as blame's set, contains a gold commit:
  `min(1, blame_count / len(lineage))` when a gold commit is in `lineage`, else 0.0; and 0.0
  when `lineage` is empty. Arm B uses it. Its size comes only from `blame_count`: no argument
  changes how the control is sized.
- **The measure** is the mean of these values over the questions, with the bootstrap and paired
  comparisons of slice B5; slice C4 computes it.

### The pre-registration in code

- **Constants:** `COVERAGE_MIN_GAIN = 0.05`, `IDENTITY_MIN_F1_GAIN = 0.05`,
  `AGREEMENT_MIN_F1 = 0.90`, `LEXICAL_CEILING = 0.35`, `REFUSAL_MIN_GAIN = 0.05`.
- **`read_thresholds(path)`** parses `PREREGISTRATION.md`'s Thresholds table, so a test can check
  the two never disagree.
- **Decision rules,** each returning the words `PREREGISTRATION.md` fixes:
  - `coverage(delta, lo)`: holds when `delta >= COVERAGE_MIN_GAIN` and `lo > 0`;
  - `selection(lo, hi)`: the reading for an interval above, containing or below 0;
  - `identity(gain, lo)`: holds when `gain >= IDENTITY_MIN_F1_GAIN` and `lo > 0`;
  - `lexical(upper)`: holds when `upper < LEXICAL_CEILING`.

  The refusal rule waits for checkpoint D, which fixes its operating point; its constant is
  here now.

### The tests' own database

- **Chosen first.** At the top of `tests/conftest.py`, before any `provenance` import,
  `PACE_DATABASE_URL` is set for the test process to `PACE_TEST_DATABASE_URL` when given, else to
  the same server with the database `pace_test`. Alembic, `connection()` and every fixture then
  use it.
- **Never `pace`.** If the chosen database is named `pace`, the session stops with an error
  naming the variable to set. CI's service database is also called `pace`, and gets a `pace_test`
  beside it like everywhere else.
- **Created when missing,** through the server's `postgres` database, then migrated by the
  existing `migrated_database` fixture.
- **Unit tests are untouched.** They never connect.

### Not in this slice

- **Blame** (C2), **lineage** (C3), **the comparison and `pace eval provenance`** (C4).
- **Refusal's decision rule:** checkpoint D.

## Acceptance Criteria

- **Scenario:** the pre-registration precedes the code
  - **Then:** `ADR-0005` and `PREREGISTRATION.md` are in this spec's commit, before any commit
    that adds blame, lineage or comparison code.

- **Scenario:** the equal-size chance
  - **Then:**
    - blame 3, lineage of 8 holding the gold: 0.375;
    - the same lineage without the gold: 0.0;
    - blame 9, lineage of 8 holding the gold: 1.0;
    - an empty lineage: 0.0;
    - `chain_hit` is 1.0 only when a gold commit is among the commits.

- **Scenario:** the thresholds in code are the ones committed in prose
  - **Then:** `read_thresholds(PREREGISTRATION.md)` equals the five constants, and a table edited
    to disagree fails the test.

- **Scenario:** the decision rules say what the pre-registration says
  - **Then:**
    - `coverage` holds at a gain of 0.05 with its lower end above 0, and fails at 0.049, and with
      a lower end of 0;
    - `selection` returns the three fixed readings;
    - `identity` and `lexical` hold and fail at their thresholds.

- **Scenario:** the tests never touch `pace`
  - **Given:** `PACE_DATABASE_URL` pointing at `pace`
  - **Then:**
    - the integration tests run in `pace_test`, created when missing;
    - a test asserts the session's `current_database()` is not `pace`;
    - `pace`'s chunks, its BM25 index state and its embedding rows are the same after a full test
      run as before it.

## Files to Modify

- In the spec commit: `docs/ADR-0005-lineage-vs-blame.md`, `docs/PREREGISTRATION.md` (new),
  `docs/tracker/004_FUNCTION_IDENTITY.md` (header)
- `provenance/eval/metrics.py`; `provenance/eval/preregistration.py` (new)
- `tests/conftest.py`
- Tests: `tests/unit/test_provenance_chain_rules.py`, `tests/unit/test_preregistration_rules.py`
  (new); an integration test that the session's database is not `pace`
- Later: `docs/tracker/004_FUNCTION_IDENTITY.md` row 1, `docs/tracker/006_EVAL_HARNESS.md` row 9,
  `README.md` and `docs/RUNBOOK.md` (where the tests run)

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests                     # integration tests land in pace_test
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    docker exec pace-db psql -U postgres -d pace -c "SELECT count(*) FROM chunks;"   # unchanged

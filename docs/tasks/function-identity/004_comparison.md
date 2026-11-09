# Task 004: The comparison — claims 1 and 2 on the 4,206 retained dev questions

**Tracker:** [004_FUNCTION_IDENTITY](../../tracker/004_FUNCTION_IDENTITY.md) row 1 and
verification step 1; [000_REVIEW_REMEDIATION](../../tracker/000_REVIEW_REMEDIATION.md) finding 3 ·
**checkpoint C**, slice C4
**Priority:** P0 · **Status:** ✅ Done (2026-11-09)
**Decision records:** [ADR-0005](../../ADR-0005-lineage-vs-blame.md) §1–§5 and §9, and its
Addenda; [`PREREGISTRATION.md`](../../PREREGISTRATION.md) claims 1 and 2. Nothing in them changes.
**Depends on:** [function-identity/001](001_preregistration.md) (the measure and the decision
rules); [symbols-blame/001](../symbols-blame/001_blame_arms.md) (arms A and L, stored);
[function-identity/002](002_lineage_engine.md) (arm C, stored);
[eval-harness/005](../eval-harness/005_gold_validity_audit.md) (the gold's validity)

## User Story

- **Summary:** Score the 4,206 retained dev questions on the arms ADR-0005 defines, built from the
  rows slices C2 and C3a stored, and decide claims 1 and 2 in the pre-registered words.

### Use Case

- **As the** author defending checkpoint C's contribution
- **I want** the comparison scored once, on committed code, from arms shown to be the ones already
  reported
- **so that** claim 1's verdict and claim 2's reading are the pre-registered test, with the volume
  split and the qualification beside them.

### Background (measured 2026-11-09; no arm has been scored against gold)

- **The questions.** 4,206 retained dev records (3,135 archaeology, 1,071 provenance), pinned to
  `1e2ad803f7`. Each has one gold commit, a full SHA, and names its function by one supporting
  anchor: 1,777 functions in all. The 1,412 distinct gold commits are all in the graph, and none
  is mechanical.
- **The arms, stored.** C2 stored blame, with plain and filtered owners, and `git log -L` for the
  1,777 functions ([results/blame_arms.json](../../../results/blame_arms.json)). Per function, arm A
  has a median of 3 commits (90th percentile 10, largest 62), unfiltered blame 3 (11, 65) and
  arm L 4 (17, 153). C3a stored lineage for the 303 in-scope files
  ([results/lineage.json](../../../results/lineage.json)): arm C has a median of 5 (17, 99). No
  function has an empty arm.
- **What exists.** `metrics.chain_hit` and `equal_size_chance` (C1) give a question's hit and arm
  B; `paired_bootstrap` (stratified), `discordant` and `mde` give the statistics;
  `preregistration.coverage` and `selection` (C1) return the pre-registered words; B5's
  `ablation.read_validity` reads the gold's validity; `generator.pool_version` names the pool.
- **The validity.** 0.9554 [0.8488, 0.9879], its labels complete, measured on pool
  `rationale-v1:commit=6810,issue_comment=18972,pull_request=5961,review_comment=38026`. The
  re-label of 2026-11-14 measures self-agreement only and does not change this rate.
- **Already decided.** Claim 3 failed (slice C3b). The agreement check with `git log -L` missed
  0.90 at 0.771 (slice C3a), and the pre-registration says that qualifies claims 1 to 3.

## Design

### Units

| Unit | Kind | Responsibility |
|---|---|---|
| `provenance/eval/comparison.py` (new) | read-only | Each question's arms from the stored rows; the cross-check; the scores; claims 1 and 2; `results/provenance.json` and `results/provenance_per_question.jsonl`. |
| `provenance/eval/runner.py` | | `pace eval provenance`. |

### The arms, per function

From the rows C2 and C3a stored, as their censuses counted them:
- **A:** the distinct filtered blame owners (`effective_commit_sha`) of the function's lines at the
  pin. A line whose filtered owner is NULL names none.
- **Blame, unfiltered** (labelled, not pre-registered, as ADR-0005's first addendum says): the
  distinct plain owners (`orig_commit_sha`) of the same lines. No gold commit is mechanical, so
  keeping mechanical commits in this row changes no hit.
- **L:** its stored `git log -L` commits, mechanical commits removed.
- **C:** the commits of every version of its identity alive at the pin, mechanical commits
  removed.

### The cross-check

Before scoring, the per-function sizes of A, unfiltered blame and L are summarised (median, 90th
percentile, largest, empty) and must equal `commits_per_function` in `results/blame_arms.json`; C's
summary must equal `arm_c_per_function` in `results/lineage.json`. Otherwise nothing is written.
So the arms scored are the arms already reported.

### A question's values

From its function's arms and its gold commit:
- **A, unfiltered blame, C and L:** `chain_hit`, 0 or 1;
- **B:** `equal_size_chance(|A|, C, gold)`, computed exactly (ADR-0005 §2).

### The statistics

- **Recall per arm:** the mean over the 4,206 questions, overall and per query class, each with
  its bootstrap interval stratified by query class.
- **Paired differences,** by `paired_bootstrap` stratified by query class, each also divided by the
  gold's validity at its point and both interval ends (ADR-0004 §8): C − A (claim 1), B − A
  (claim 2), C − B (the share due to volume), L − A (reported) and unfiltered blame − A
  (labelled, not pre-registered).
- **D and the MDE** beside each difference between two 0-or-1 arms: C − A, L − A and unfiltered
  blame − A.
- **Arm sizes per question:** |A|, |C| and |L|, as median, 90th percentile and largest.

### The decisions

- **Claim 1:** `preregistration.coverage` on C − A and its lower end: "holds", or the
  pre-registered sentence.
- **Claim 2:** `preregistration.selection` on B − A's interval: one of the three pre-registered
  readings.
- **The split:** C − A = (C − B) + (B − A), reported whatever its sign (ADR-0005, Consequences).
- **The qualification:** the agreement check's mean F1 and verdict, copied from
  `results/lineage.json`, so they travel with the result.

### The report

- **`results/provenance.json`:** the inputs (this repository's commit, `PREREGISTRATION.md`'s last
  commit, the golden set's SHA-256, the pool version, blame's and `git log -L`'s recorded
  commands, the detector `lineage-mvp-1`, and the validity used); the cross-check; the
  statistics; the decisions.
- **`results/provenance_per_question.jsonl`:** one line per question: its qid, query class,
  function, gold commit, |A| and |C|, and each arm's value. Every number in the first file can be
  recomputed from it. It holds no question or commit text (DATA-PROTECTION.md §3).

Two runs write the same bytes.

### The gate

`pace eval provenance` writes nothing unless:
- no tracked file is modified, so the recorded commit is the code that ran, and
  `docs/PREREGISTRATION.md` is committed (ADR-0005 §9 asks for the second; the first is slice
  C3b's rule, kept);
- every question's function is a function at the pin, with stored blame for every line, stored
  `git log -L`, and an identity alive at the pin;
- the cross-check passes;
- the gold's validity is current: the audit's labels are complete and were measured on the
  current pool.

### The run, once

- **When:** on the slice's branch, after its code and tests are committed.
- **Once:** repeated once unchanged to show the same bytes, then committed.
- **After it:** a defect found in this slice's code is fixed and the run repeated, and both runs
  are reported, the first kept in git history.

### Not in this slice

- The test split's re-measure of claims 1 and 2, at the project's final opening with claim 4. It
  needs blame stored for the test split's functions first.
- Checkpoint D.

## Acceptance Criteria

- **Scenario:** the arms are the reported ones
  - **Then:** a summary that differs from `results/blame_arms.json` or `results/lineage.json`
    writes nothing.

- **Scenario:** a question's values
  - **Then:** a gold commit in A and C scores 1 in both; one only in C scores 0 in A and 1 in C; one
    in neither scores 0 everywhere. B is min(1, |A| / |C|) when C holds the gold, and 0 otherwise.

- **Scenario:** the decisions
  - **Then:** claim 1's verdict is `coverage`'s words and claim 2's reading `selection`'s, and the
    reported C − A equals (C − B) + (B − A).

- **Scenario:** the fixture
  - **Then:** on C3a's fixture repository, with hand-made questions, every per-question value and
    the report are as expected.

- **Scenario:** the gate
  - **Then:** a modified tracked file, a question whose function has no stored blame or no
    identity, a changed census summary, and a stale or incomplete validity each write nothing.

- **Scenario:** determinism
  - **Then:** two runs write the same bytes.

## Files to Modify

- In the spec commit: `docs/tracker/004_FUNCTION_IDENTITY.md` (header)
- New: `provenance/eval/comparison.py`
- `provenance/eval/runner.py`
- Tests: unit rules for a question's values, the decisions and the cross-check; an integration test
  on C3a's fixture repository; the CLI surface
- `results/provenance.json` and `results/provenance_per_question.jsonl` (new, committed)
- Later: trackers 000 and 004, the ROADMAP's status, `results/README.md`, `docs/RUNBOOK.md`, and
  ADR-0005's Addenda

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    uv run pace eval provenance        # once, then again unchanged: the same bytes

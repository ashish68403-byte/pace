# Pre-registration

Committed on 2026-11-08 with [ADR-0005](ADR-0005-lineage-vs-blame.md), before any blame,
lineage or comparison number exists. `pace eval provenance` refuses to run while this file is
uncommitted or modified, and records the commit it ran under, so `git log` shows this file
preceding every comparison result.

**Rules.** This file is never edited to fit a result. A change is a dated amendment, added below
and committed before the run it affects, saying what changed and why. A claim that fails is
reported as failed, in the words given for it here.

## Thresholds

The code holds these as constants, and a unit test checks they equal this table.

| Constant | Value | Used by |
|---|---|---|
| `COVERAGE_MIN_GAIN` | 0.05 | Claim 1 |
| `IDENTITY_MIN_F1_GAIN` | 0.05 | Claim 3 |
| `AGREEMENT_MIN_F1` | 0.90 | The agreement check |
| `LEXICAL_CEILING` | 0.35 | Claim 4 |
| `REFUSAL_MIN_GAIN` | 0.05 | Claim 5 |

## Claims

Every interval is a 95% paired bootstrap unless named otherwise. Claims 1 and 2 are decided on
the 4,206 retained dev records and re-measured once on the test split, at its final opening,
reported beside.

### 1. Coverage — the primary claim

- **Measurement.** Provenance-chain recall (ADR-0005 §3), lineage in full (arm C) against blame
  (arm A).
- **Prediction.** C > A: blame names only the last commit to touch each line, and misses changes
  whose lines were later overwritten.
- **It holds if** C − A ≥ `COVERAGE_MIN_GAIN` and the interval's lower end is above 0.
- **If it fails, I will write:** "Walking the function's lineage did not recover measurably more
  rationale than git blame on this benchmark."

### 2. Selection — the volume-matched control

- **Measurement.** Arm B (lineage, the exact chance of a random subset as large as blame's)
  against arm A.
- **Prediction.** B ≤ A. Every question's gold lies in its function's current name and file
  (ADR-0005, the tilt), so lineage's older commits can only dilute B.
- **Reading, fixed now.** An interval above 0: lineage picks better commits even on a set tilted
  against it. An interval containing 0: lineage's gain is volume. An interval below 0: blame is
  more precise per commit, and lineage's gain is coverage alone. All three are reported in these
  words, with C − A split into C − B and B − A.

### 3. Identity — on CodeShovel's oracle

- **Measurement.** Commit-set F1 per function on the 40 Python functions of CodeShovel's oracle,
  lineage against `git log -L`, paired over the functions.
- **Prediction.** Lineage > `git log -L`, most of all on the 16 functions holding a rename or move.
- **It holds if** the mean F1 gain ≥ `IDENTITY_MIN_F1_GAIN` and the interval's lower end is above
  0. The 16 rename-or-move functions are reported apart and decide nothing.
- **If it fails, I will write:** "The lineage layer did not track Python functions measurably
  better than git log -L on CodeShovel's oracle."

### 4. The benchmark stays out of grep's reach — on the test split

- **Measurement.** ADR-0003 §6's gate on the retained test records at the test split's final
  opening: the strongest lexical baseline's Wilson 95% upper bound on recall@10.
- **Prediction.** Below the ceiling, as on dev (dev: 0.151).
- **It holds if** the upper bound < `LEXICAL_CEILING`.
- **If it fails, I will write:** "On the test split, a lexical baseline reaches the gold often
  enough that the benchmark does not separate retrieval from string matching."

### 5. Refusal buys accuracy — checkpoint D

- **Measurement.** Accuracy on the questions the agent answers, at the operating point checkpoint
  D's decision record fixes on dev before D's first run, against the same agent answering
  everything; and the risk–coverage curve against two trivial abstainers: always answer, and
  abstain at random at the same coverage.
- **Prediction.** Refusing raises accuracy on what is answered, and the curve lies below both
  abstainers.
- **It holds if** the gain ≥ `REFUSAL_MIN_GAIN`, with non-overlapping Wilson 95% intervals, and
  the curve lies below both abstainers at the operating point.
- **If it fails, I will write:** "Refusal did not buy measurable accuracy over answering
  everything."

## Checks, not claims

- **Lineage agrees with `git log -L` on Airflow.** On 100 stratified functions, commit-set F1 ≥
  `AGREEMENT_MIN_F1`, reported apart for spans `git log -L` follows across the 2025 directory
  move and spans it does not. Missing it is reported, and it qualifies claims 1 to 3.

## Amendments

None.

# Task 002: Add a dev/test split before CI has seen the whole golden set

**Tracker:** [000_REVIEW_REMEDIATION](../../tracker/000_REVIEW_REMEDIATION.md) · finding 2
**Priority:** P0 · **Severity:** major · **Status:** ⬜ Not started
**Deadline:** before the week-3 generator produces records at scale. Not repairable afterwards.

## User Story

- **Summary:** Every tuning decision in weeks 6-12 is selected against the same golden set the
  headline number is reported from. Add `split` to `GoldenRecord` now, while the set has 3 records
  and this is a schema bump rather than a re-labelling exercise.

### Use Case

- **As the** author reporting one ablation table as the contribution
- **I want** the configuration chosen on a dev half and reported on a test half opened twice
- **so that** the headline lift is not the winner's curse, and the dates of those two openings are
  visible in git history.

### Background (measured)

`GoldenRecord` (`provenance/eval/schema.py:196-217`) has no `split` field under `extra="forbid"`;
`pace eval run` exposes one `--golden` path; `grep -rniE "hold-?out|dev set|train/test"` over
`docs/` and `provenance/` returns only `S3_HELD_OUT`, which is an unanswerable-construction
strategy, not a split.

The project already knows the rule. `provenance/retrieve/fusion.py:139-143`:

> Tuning k on the same queries you then report on is how a project reports its own overfitting
> as a result.

— and then offers six `k` candidates from that same function. Weeks 6-12 select chunk size,
overlap, RRF k, fusion weights, rerank depth, blame flags and two hash tiers; 20-50 comparisons is
a conservative count.

**The size of the problem.** At n=200 with every configuration at identical true quality p=0.6, the
winner's measured recall exceeds its true value by:

| Configs compared | Optimistic bias |
|---|---|
| 5 | +4.0 points |
| 20 | +6.4 points |
| 50 | +7.7 points |

That is the same magnitude as the lift being claimed.

### Acceptance Criteria

- **Scenario:** The field exists before the data does
  - **Given:** the golden set still has 3 records
  - **When:** `GoldenRecord` gains `split: Literal["dev", "test"]`
  - **Then:** it is a required field under `extra="forbid"`, assigned at generation time, and the
    3 existing records are migrated in the same commit.

- **Scenario:** The split does not leak gold across the boundary
  - **Given:** several queries can anchor on one PR or one file
  - **When:** the generator assigns splits
  - **Then:** it partitions **grouped by commit SHA / PR number**, not randomly by query, and
    stratifies by `query_class` to preserve the ~20% unanswerable share.
  - **And:** it is not a temporal split — on this corpus a time split confounds with the 2025
    directory move (ROADMAP Trap 2).

- **Scenario:** The gate only ever sees dev
  - **Given:** the CI retrieval gate and any `tune_k` helper
  - **When:** either runs
  - **Then:** both read dev only, and dev is the larger half (~60/40), because that is where the
    power-sensitive gate lives.

- **Scenario:** Test cannot be used to update the baseline
  - **Given:** a run scored on the test split
  - **When:** `--update-baseline` is passed
  - **Then:** it is refused with a hard error naming the split.

- **Scenario:** Test openings are auditable
  - **Given:** the test split exists
  - **When:** it is scored
  - **Then:** it has been opened at most twice across the project, and each opening is a dated
    commit — so "did you look before you chose?" is answered by `git log`, not by memory.

## Files to Modify

- `provenance/eval/schema.py` — `GoldenRecord` gains `split`
- `provenance/eval/runner.py` — split-aware loading; refuse `--update-baseline` on test
- `provenance/retrieve/fusion.py` — point any k-tuning at dev
- `provenance/eval/golden/ci_subset.jsonl` — migrate the 3 records
- `provenance/eval/README.md` — document the split and the two-openings rule

## Notes

- Group-wise splitting matters more here than the split itself. A random by-query split leaks: two
  queries mined from the same PR put that PR's gold document on both sides of the boundary, so the
  "held-out" half is partly memorised. Group by the artifact the gold comes from.
- Pair with task 007 (finding 7): the MDE calculation decides the generator's target `n`, and the
  split decides how that `n` divides. Decide both in week 3 or neither.

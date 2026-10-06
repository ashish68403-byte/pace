# Task 003: Give the week-8 comparison a control, a defined dependent variable, and a falsifier

**Tracker:** [000_REVIEW_REMEDIATION](../../tracker/000_REVIEW_REMEDIATION.md) · finding 3
**Priority:** P0 · **Severity:** critical · **Status:** ⬜ Not started
**Deadline:** before week 8 runs. A control arm is an experiment, not a paragraph.

## User Story

- **Summary:** The thesis contributes one ablation table — lineage on versus blame-only. The
  lineage arm reaches 4-6× more commits than the blame arm, so the comparison is confounded by
  construction, and the dependent variable it reports does not exist in code.

### Use Case

- **As the** author answering "how much of that gain is just indexing more history?"
- **I want** a volume-matched control arm and a dependent variable defined in `metrics.py`
- **so that** the examiner's obvious rival explanation was measured rather than argued away.

### Background (measured)

`grep -rni "provenance.chain|chain_recall" --include=*.py` returns **nothing**. `metrics.py`
defines `recall_at_k`, `mrr`, `ndcg_at_k`, `citation_precision` and the two refusal metrics — so
the thesis's single novel dependent variable has no implementation and no written definition, which
means nothing fixes the per-query candidate budget the comparison depends on.

The confound is established by the roadmap's own numbers: `models/dag.py` at 95 commits by path
versus **611** with rename-following (6.4×, ROADMAP:128), and 5,978 versus 1,479 in-scope commits
(4×, ROADMAP:131).

Separately, ROADMAP:197's gate is *"Ablation beats the baseline measurably"* — a gate whose pass
condition is its own hypothesis. And `grep -rni "pre-regist|falsif"` across `docs/` and
`provenance/` returns one unrelated line in a migration. Every threshold in this project is
pre-committed in code — `RETAINED_GREP_R10_MAX = 0.35`, `BOOTSTRAP_SEED = 20250131`,
`MIN_QUERIES_FOR_PAIRED_GATE = 100` — except the ones that decide whether the thesis is true.

### Acceptance Criteria

- **Scenario:** The dependent variable exists
  - **Given:** `metrics.py`
  - **When:** provenance-chain recall is implemented
  - **Then:** its definition fixes an **equal evidence budget per query** — the same number of
    candidate commits scored in every arm — and that budget is a named constant, not an argument
    default.

- **Scenario:** The comparison has a volume-matched control
  - **Given:** week 8's run
  - **When:** the ablation executes
  - **Then:** it runs **three** arms, not two:
    - **A — blame-only.** The baseline.
    - **B — lineage, volume-matched.** Equal evidence budget to A, so any difference is
      attributable to *which* commits lineage selects, not how many.
    - **C — lineage, unbudgeted.** The full system, reported separately.
  - **And:** B is the arm the novelty claim rests on; C is reported as the deployed configuration.

- **Scenario:** The falsifier predates the result
  - **Given:** week 8 has not yet run
  - **When:** `docs/PREREGISTRATION.md` is committed
  - **Then:** for each of the three claims — lineage beats the volume-matched control, refusal buys
    accuracy, grep stays below 0.35 — it states the predicted direction, the minimum effect worth
    claiming, and the outcome that would make the author write that the claim failed.
  - **And:** its commit date precedes the first week-8 ablation run, visibly, in `git log`.

- **Scenario:** The gate stops asserting its own hypothesis
  - **Given:** ROADMAP:197
  - **When:** the week 9-10 gate is reworded
  - **Then:** it names the measurement and the pre-registered threshold, not the desired outcome.

## Files to Modify

- `provenance/eval/metrics.py` — define provenance-chain recall with an explicit evidence budget
- `provenance/eval/runner.py` — three-arm ablation support
- `docs/PREREGISTRATION.md` — new, committed before week 8
- `docs/ROADMAP.md` §5 week 8 and week 9-10 gates

## Notes

- A fourth **placebo arm** (identical edge count and degree distribution, targets rewired to random
  same-era commits) would be the strongest possible control. The review explicitly judged it beyond
  a solo 14-week budget and called three arms the right call — record that you considered and
  rejected it, with the reason. A deliberate measured omission scores; a silent one does not.
- Pre-registration is cheap insurance with an asymmetric payoff: it costs an afternoon, and it
  converts "did you decide that before or after you saw the number?" from the worst question in the
  viva into a one-line answer with a commit hash.

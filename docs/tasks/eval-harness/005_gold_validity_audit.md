# Task 005: Measure how much of the benchmark's gold explains its function

**Tracker:** [006_EVAL_HARNESS](../../tracker/006_EVAL_HARNESS.md) row 6 · **checkpoint B**, before
slice B5's ablation rows; closes review finding 6 ([000](../../tracker/000_REVIEW_REMEDIATION.md))
**Priority:** P0 · **Status:** ⬜ Not started
**Decision records:** [ADR-0004](../../ADR-0004-gold-validity-audit.md), new in the spec commit;
[ADR-0003](../../ADR-0003-rationale-pool-benchmark.md) §4 (what gold is) and §7 (the split's hash
rule)
**Depends on:** [003](003_golden_set_generator.md) (the golden set and the probe) and
[004](004_github_backfill.md) (PR text in gold, and the regenerated set)

## User Story

- **Summary:** Label a stratified, pre-registered sample of 56 retained dev records by whether
  their gold text gives the reason for the function's change. Estimate the dev set's validity rate
  with an interval, measure how much rationale PR text adds to commit messages, and re-label 15
  records a week later for self-agreement. Beside it, report two measurements that need no
  labeller: how much gold no cheap baseline reaches at k = 100, and whether commits without a PR
  link differ from those with one.

### Use Case

- **As the** author defending checkpoint B's benchmark
- **I want** a measured share of records whose gold explains its function, with an interval and
  published per-record labels
- **so that** "is your gold any good?" has a committed answer, and every lift B5 reports can be
  read against it.

### Background (measured 2026-10-31)

- **The premise is unmeasured** (finding 6). ADR-0003 takes as gold every pool document of a
  change; nothing checks that the text explains the function the question names. The retention
  filters and the gate both catch gold that is too easy, not gold that is wrong.
- **The dev set:** 4,206 retained records. 1,034 provenance and 3,083 archaeology records have a
  linked PR; 37 and 52 do not.
- **What gold is, per record:** the commit message, plus, for a PR-linked record, the PR
  description and every human review and conversation comment of the PR. A PR has a median of 4
  such comments (90th percentile 25, 99th 93, maximum 277).
- **The linkage-bias warning predates ADR-0003.** The review feared a benchmark made only of
  commits with a recoverable PR link (Bird et al. 2009). ADR-0003 generates questions from every
  qualifying commit, linked or not. The open question is whether the unlinked ones differ.
- **The building blocks exist:**
  - `metrics.wilson_interval`;
  - `leakage.PoolIndex` and its rankings, and `leakage.resolve_gold_from_db`;
  - `parse.symbols.symbols`, which returns a file's functions by qualified name;
  - `file_revisions`, which keeps every revision's blob and its path at the time;
  - the split's hash rule, `schema.assign_splits`.
- **Decisions made with the user, 2026-10-31:**
  - dev only;
  - strata by query class × PR-linked;
  - a resumable terminal tool that shows the commit message and PR description in full, and the
    comments on request;
  - the re-label as its own command, run at least a week later;
  - both automatic companions now;
  - allocation by stratum size with 6 per commit-only stratum, and a two-step label for PR-linked
    records. This replaced equal allocation once it was measured: equal allocation would leave the
    overall rate at about ±19 points.

## Design

### Units

| Unit | Kind | Responsibility |
|---|---|---|
| `provenance/eval/gold_validity.py` | pure, then files | The strata and allocation; drawing the sample; the label records and their files; the staleness check; the report. `pace audit sample` and `pace audit report`. |
| `provenance/eval/labelling.py` | DB and git, then terminal | One record's view (its gold documents by kind, and the function's change) and the resumable two-step session behind `pace audit label` and `pace audit relabel`. |
| `provenance/analysis/linkage_bias.py` | DB, then pure | Linked against unlinked commits, by module, author and year. |
| `provenance/eval/leakage.py` | pure | `reachable_at_k`: does any lexical baseline's top *k* hold a gold document. |
| `provenance/eval/metrics.py` | pure | `cohens_kappa` and `stratified_proportion`; `wilson_interval` accepts a fractional count, for an effective sample size. |

### The files

| File | Written by | Holds |
|---|---|---|
| `provenance/eval/audit/gold_validity_sample.json` | `pace audit sample`, once | The pre-registration: seeds, pool version, corpus commit, each stratum's population and sample size, and per record its qid, stratum, presentation order, re-label order (or none) and content hash. |
| `provenance/eval/audit/gold_validity_labels.jsonl` | `pace audit label` | One line per labelled record. |
| `provenance/eval/audit/gold_validity_relabels.jsonl` | `pace audit relabel` | The same, for the 15-record subset. |
| `results/gold_validity_audit.json` | `pace audit report` | The report: sorted keys, no timestamps. |

A label line holds:

- `qid` and `stratum`;
- `message_label` (the commit message alone) and `label` (the final label);
- `note`, optional;
- `labelled_on`, a date;
- `read_change`, whether the function's change was opened;
- `read_comments`, whether the PR's comments were opened (`null` for a commit-only record).

Labels are words (`rationale`, `mentions`, `unrelated`), so the file reads without a key.

### The sample (ADR-0004 §2)

- **Population:** retained dev records: `passes_leakage_filter`, split `dev`, not hand-written.
- **Strata and sizes,** as constants: provenance PR-linked 11, archaeology PR-linked 33,
  provenance commit-only 6, archaeology commit-only 6. The command refuses if a stratum holds
  fewer records than its size.
- **Which, and in what order:** ADR-0004 §2's seeded hash rules pick the records, their
  presentation order, the 15-record re-label subset, and its order.
- **Once:** `pace audit sample` refuses to overwrite an existing sample. It prints the strata and
  the path. The sample is committed before the first label.

### The labelling tool (ADR-0004 §3–§4)

- **The view:**
  - a header with the record's place in the session, its stratum and its qid;
  - the question;
  - the function: today's path and its qualified name;
  - the commit: short sha and date;
  - the commit message, in full.
- **On request,** at any prompt:
  - `d` shows the function's change. For an `added` record, that is the function's source at the
    commit. For a `changed` record, it is a unified diff of the function's source between the
    previous revision of its file and this one. Both come from `file_revisions` blobs, read
    through `parse.symbols.symbols`.
  - `?` prints the codebook.
  - `q` quits; the record on screen is not written.
- **First label:** `1` rationale, `2` mentions, `3` unrelated, for the commit message alone.
- **Second label,** for a PR-linked record whose first label is not `rationale`:
  - the PR description in full;
  - a line counting the PR's review and conversation documents, with `c` to read them;
  - a prompt that accepts only labels claiming at least as much as the first.

  A first `rationale` is also the final label. A commit-only record's single label is both.
- **Then** an optional one-line note, and the line is appended to the labels file.
- **Resumable.** A record already in the labels file is skipped, so a restarted session continues
  at the first unlabelled record in presentation order.
- **Nothing is suggested.** The tool shows no default label, no earlier label and no model output.
  Invalid input asks again.

### The re-label (ADR-0004 §6)

- `pace audit relabel` runs the same view and the same two steps over the 15-record subset, in its
  own order, and appends to the relabels file. It resumes the same way.
- It refuses to start until every subset record has a first label, and until at least seven days
  have passed since the latest `labelled_on` among them.
- It never reads a first label for display: not the label, not the note.

### The report (ADR-0004 §5–§7)

- **`sample`:** the pool version, the corpus commit, the seeds, and each stratum's population and
  sample size.
- **`labels`:** `{"status": "incomplete", "labelled": n, "of": 56}` while any record is
  unlabelled, and nothing else: a partial sample breaks the strata sizes. Once complete:
  - per stratum: the count of each final label, the validity rate and its Wilson interval, and the
    same for the commit message alone;
  - overall: the stratified validity rate, its interval at the effective sample size, the
    effective sample size, and the stratified shares of `mentions` and `unrelated`;
  - what PR text adds: over the PR-linked strata, the validity of the commit message alone, the
    final validity, and the number of records whose rationale only the PR text supplied;
  - reading: how often the function's change and the PR's comments were opened.
- **`self_agreement`:** `{"status": "not_measured"}` until all 15 relabels exist. Then: n,
  observed agreement and Cohen's kappa for the final label, and the same for the commit message
  alone. A kappa whose chance agreement is 1 (every label the same) is `null`, with its reason.
- **`reachability`:**
  - k = 100, the three lexical baselines named, and dense marked not measured;
  - the number of records, how many no baseline reaches, and that share, overall and per stratum.
- **`linkage_bias`:**
  - each group's commit count;
  - per module, each group's distribution: each commit spreads a weight of 1 equally over the
    modules it touched;
  - each group's distinct authors, its top-ten share, and the overlap of the two top tens;
  - per year, each group's commits;
  - the total-variation distance for modules and for years;
  - no author name.
- `pace audit report` prints a short summary and writes the file; `--out` writes it elsewhere.

### Staleness (ADR-0004 §9)

`pace audit label`, `relabel` and `report` compare the sample with what is stored now. Each
sampled qid must exist in `provenance/eval/golden/auto_full.jsonl` with the same content hash, and
`generator.pool_version` must equal the sample's. Otherwise the command refuses and names the
first difference.

### Stages

1. **This plan:** the code and its tests. Then, on `pace`, `pace audit sample`, committed; and one
   `pace audit report --out` run outside the repository, to check the companions on real data.
2. **The labelling, the author's hour:** the 56 labels and the report, committed.
3. **At least seven days later:** the 15 relabels and the regenerated report, committed. Finding 6
   closes.

### Not in this task

- **The test split:** never opened for this.
- **Applying the validity rate to lifts:** slice B5 quotes lifts twice (ADR-0004 §8) when it
  reports them.
- **Dense retrieval in the reachability check:** when frozen vectors exist.
- **A new sample after the golden set or the pool changes:** drawn then, as ADR-0004 §9 requires.

## Acceptance Criteria

- **Scenario:** the sample is drawn once, the same way every time
  - **Given:** a golden set whose dev records fill all four strata
  - **Then:**
    - each stratum's sample has its fixed size, and two draws are identical;
    - the presentation order interleaves the strata;
    - the re-label subset is 15 of the sampled records;
    - a second `sample` refuses to overwrite;
    - a stratum smaller than its size refuses.

- **Scenario:** a record's view shows its gold and its function's change
  - **Given:** `history_repo` with its pool, its PRs fetched from a fake GitHub, and its golden
    records
  - **Then:**
    - `helper`'s change at c7 shows c7's message, PR 14's description, a count of one
      conversation comment, and a diff of `helper` alone;
    - `alpha`'s addition at c1 (a commit-only record) shows c1's message and `alpha`'s source.

- **Scenario:** two-step labelling, resumable, never suggesting
  - **Given:** canned answers to the prompts
  - **Then:**
    - a first `rationale` asks once, and writes `rationale` for both labels;
    - after a first `mentions`, the final prompt accepts only `rationale` or `mentions`;
    - a commit-only record asks once;
    - `c` and `d` show text, and set `read_comments` and `read_change`;
    - `q` writes nothing for the record on screen, and the next session starts there;
    - invalid input asks again.

- **Scenario:** the re-label waits a week, and shows nothing of the first pass
  - **Then:** it refuses before seven days, and while a subset record lacks a first label. Once
    allowed, the first pass's note never appears in its output.

- **Scenario:** the statistics are right
  - **Then:**
    - `cohens_kappa` matches a hand-computed example, and is `None` when chance agreement is 1;
    - `stratified_proportion` matches a hand computation, gives Wilson's interval at n − 1 for a
      single stratum of a very large population (Cochran's unbiased variance), and uses the
      sample size when the variance is 0.

- **Scenario:** the report
  - **Then:**
    - with 55 labels it says incomplete, and gives no rate;
    - with 56 it gives every stratum, the overall rate and what PR text adds;
    - without relabels, self-agreement is not measured;
    - two runs write the same bytes.

- **Scenario:** a changed golden set or pool stops the audit
  - **Given:** a sampled record whose question changed, and separately a different pool version
  - **Then:** `label`, `relabel` and `report` refuse, and name the difference.

- **Scenario:** the companions
  - **Then:**
    - a gold document a baseline ranks 50th is reachable at k = 100 and not at k = 10;
    - on `history_repo`, the reachability counts agree with the probe's own rankings;
    - modules bucket by today's path, with `(top level)`;
    - the total-variation distance is 0 for identical distributions and 1 for disjoint ones;
    - the linkage-bias output contains no author name.

- **Scenario:** the real run
  - **Given:** `pace`
  - **Then:** `pace audit sample` draws 11, 33, 6 and 6 records, and the sample is committed;
    `pace audit report` runs, with labels incomplete and both companions computed.

- **Scenario:** the plan documents agree, at stage 3
  - **Then:** tracker 000 row 6 and tracker 006 row 6 are ✅; ROADMAP shows the audit;
    `results/README.md` lists the report; RUNBOOK describes the labelling and the re-label.

## Files to Modify

- `provenance/eval/gold_validity.py`, `provenance/eval/labelling.py`,
  `provenance/analysis/linkage_bias.py`: new
- `provenance/eval/leakage.py` (`reachable_at_k`), `provenance/eval/metrics.py` (`cohens_kappa`,
  `stratified_proportion`, `wilson_interval`'s types), `provenance/cli.py` (`pace audit`)
- `provenance/eval/audit/`: the sample (stage 1), the labels (stage 2), the relabels (stage 3)
- `results/gold_validity_audit.json`: new at stage 2, regenerated at stage 3
- Tests: `tests/unit/test_gold_validity_rules.py`, `tests/unit/test_labelling_rules.py`,
  `tests/unit/test_linkage_bias_rules.py`, `tests/integration/test_gold_validity.py` (new);
  `tests/unit/test_metrics.py`, `tests/unit/test_cli_surface.py`
- In the spec commit: `docs/ADR-0004-gold-validity-audit.md`, `docs/tracker/006_EVAL_HARNESS.md`,
  and ROADMAP's list of decision records
- Later: `docs/ROADMAP.md`, `docs/RUNBOOK.md`, `docs/tracker/000_REVIEW_REMEDIATION.md`,
  `results/README.md`

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests -q
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    uv run pace audit sample        # stage 1, once
    uv run pace audit label         # stage 2, the author; resumable
    uv run pace audit report
    uv run pace audit relabel       # stage 3, at least seven days later
    uv run pace audit report

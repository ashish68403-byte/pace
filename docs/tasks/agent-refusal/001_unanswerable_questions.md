# Task 001: The unanswerable questions — S3, S4 and S5 negatives for checkpoint D's refusal test

**Tracker:** [007_AGENT_REFUSAL](../../tracker/007_AGENT_REFUSAL.md) tasks 3 and 4 · **checkpoint
D**, slice D1
**Priority:** P0 · **Status:** ✅ Done (2026-11-09)
**Decision records:** [ADR-0006](../../ADR-0006-refusal-first-checkpoint-d.md) §2, in this spec's
commit; [ADR-0003](../../ADR-0003-rationale-pool-benchmark.md) (the answerable benchmark, its
templates and splits)
**Depends on:** [eval-harness/003](../eval-harness/003_golden_set_generator.md) (the golden set);
[function-identity/002](../function-identity/002_lineage_engine.md) (the names lineage has seen)

## User Story

- **Summary:** Build checkpoint D's unanswerable questions: 300 dev and 214 test questions of each
  of three kinds, worded exactly like the answerable ones, in their own golden file with a census,
  so that slice D3 can measure refusal per kind.

### Use Case

- **As the** author testing claim 5, that refusal buys accuracy
- **I want** negatives that only missing evidence distinguishes from the answerable questions
- **so that** refusal is measured where it matters, and the easier kinds are reported apart.

### Background (measured 2026-11-09)

- **The golden set.** `provenance/eval/golden/auto_full.jsonl` holds 8,250 records, 7,199
  retained. Dev has 4,206 (3,135 "changed", 1,071 "added") over 1,777 functions in 215 files; test
  has 2,993 over 1,414 functions in 203 files. All are answerable. Its SHA-256 is recorded in
  `results/blame_arms.json`, `results/lineage.json` and `results/provenance.json`.
- **The schema.** `UnanswerableStrategy` names S1 to S5, and `QueryClass.UNANSWERABLE` exists. An
  unanswerable record needs a strategy and no required evidence. A generated record without a
  leakage probe is rejected: the validator does not exempt unanswerable records, although
  `passes_leakage_filter` retains every one.
- **The pin.** `1e2ad803f7`, committed on 10 Sep 2026.
- **The names.** `symbol_versions` holds 5,073 qualified names across the 303 files' histories.
- **The pool.** 69,769 rationale documents in `chunks`: commit messages, pull request
  descriptions, and review and conversation comments (pool
  `rationale-v1:commit=6810,issue_comment=18972,pull_request=5961,review_comment=38026`).

## Design

### Units

| Unit | Kind | Responsibility |
|---|---|---|
| `provenance/eval/unanswerable.py` (new) | read-only on the database | Builds the three kinds from the retained records; the S5 name check; writes `provenance/eval/golden/unanswerable.jsonl` and `results/unanswerable_census.json`. |
| `provenance/eval/schema.py` | | `GoldenRecord.held_out`, an optional `GoldEvidence` allowed only on S3 records; a generated unanswerable record may carry no leakage probe. |
| `provenance/cli.py` | | `pace golden unanswerable`. |

### The source questions

Only retained records, one split at a time. Each kind ranks its split's retained records by the
SHA-256 of `"pace-unanswerable-v1:<kind>:" + qid` and takes them in that order:
- **S3:** the first 300 on dev and the first 214 on test. These are held back from slice D3's
  answerable sample.
- **S4:** the records whose function no earlier S4 question uses, dev before test, until 300
  and 214 functions: each function is asked about after the pin once in the file.
- **S5:** the records in order, one name each, until 300 and 214 names pass the check.

### The three kinds

- **S3, held out.** The source's question, unchanged. Its required evidence, the commit and any
  pull request, becomes the record's `held_out`. The supporting anchor is the source's function.
- **S4, after the pin.** "Why did `X` change in <Month YYYY>?" for the source's function `X`. The
  month is October, November or December 2026, by the record's hash modulo 3. The supporting
  anchor is the function.
- **S5, nonexistent.** The source's template, file and, for "changed", month, with a new name:
  - **The splice.** The partners are the other functions lineage holds alive at the pin in the
    same file, ranked by the SHA-256 of `"pace-unanswerable-v1:s5:" + qid + ":" + partner`. The
    source function's short name and a partner's are split at underscores. The new name is the
    first half of the source's parts (rounded up) joined to the second half of the partner's
    (rounded down, at least one), a leading underscore kept: `_get_dag_runs` and `update_state`
    give `_get_dag_state`. A method keeps its class: `Class.<new name>`. A name of underscores
    alone, such as `_`, has no parts, and a pair with one is not spliced.
  - **The check.** A name is used only if no name lineage has seen ends in the new short name
    (as its whole last part, so neither the name nor a namesake in another class exists), no
    earlier S5 question uses it, dev before test, and no rationale document contains the short
    name, ignoring case. Otherwise the next partner is tried, then the next source record.
  - The supporting anchor is the file, with no qualified name.

### The records

- **Fields:** query class `unanswerable`; `unanswerable_strategy` `s3_held_out`, `s4_temporal` or
  `s5_nonexistent`; empty required evidence; `generation_strategy` `unanswerable_v0`; the pin as
  `corpus_commit`; the source's split; no leakage probe.
- **Ids:** `s3-`, `s4-` or `s5-` followed by the source's qid.
- **The file:** `provenance/eval/golden/unanswerable.jsonl`, a comment header like
  `auto_full.jsonl`'s, then one record per line in qid order.

### The census

`results/unanswerable_census.json` holds:
- **inputs:** the golden set's SHA-256, the pin, the pool version and the salt;
- **per split and kind:** the count, the template mix, and how many source records were read;
- **for S5:** pairs not spliced, splices tried, and splices rejected as seen by lineage, as
  already used and as found in the pool.

Two runs write the same bytes. It holds no question text.

### Refusals

`pace golden unanswerable` writes nothing when the golden set has no retained record, when
lineage has not been built for the repository, or when any kind cannot reach its count.

### The hiding contract, for slice D2

For an S3 question, retrieval returns no pool document whose `chunk_evidence` links it to a
held-out commit or pull request.

### Not in this slice

- Hiding in retrieval, the pipeline and the answerer (D2).
- The operating point, the evaluation sample and claim 5's run (D3).
- S1 and S2 (ADR-0006 §2).

## Acceptance Criteria

- **Scenario:** the decision record precedes the code
  - **Then:** ADR-0006 is in this spec's commit, before any commit that adds D1's code.

- **Scenario:** the three kinds
  - **Then:** on a fixture, S3 keeps its source's question and moves its evidence to `held_out`;
    S4 asks about a month after the pin; S5's name exists in no name lineage has seen and in no
    pool document.

- **Scenario:** the schema
  - **Then:** an unanswerable record loads without a leakage probe; `held_out` on a record that is
    not S3 is rejected; every answerable record still loads unchanged.

- **Scenario:** the S5 check
  - **Then:** a splice lineage has seen, or the pool mentions, is rejected and counted.

- **Scenario:** refusals
  - **Then:** before a lineage build, or when a kind falls short, nothing is written.

- **Scenario:** determinism and the benchmark
  - **Then:** two runs write the same bytes, and `auto_full.jsonl` does not change.

## Files to Modify

- In the spec commit: `docs/ADR-0006-refusal-first-checkpoint-d.md` (new), `docs/ROADMAP.md` (§5
  D's row, §6's first cut), trackers 007, 002 and 000 (finding 16)
- New: `provenance/eval/unanswerable.py`
- `provenance/eval/schema.py`, `provenance/cli.py`
- Tests: unit rules for the source choice, the splice and the schema; an integration test on a
  fixture with lineage and a few pool rows
- `provenance/eval/golden/unanswerable.jsonl` and `results/unanswerable_census.json` (new,
  committed)
- Later: tracker 007, `results/README.md`, `docs/RUNBOOK.md`

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    uv run pace golden unanswerable        # twice: the same bytes

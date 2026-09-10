# ADR-0002: The anchor symbol comes from the code, never from the rationale

- **Status:** Accepted
- **Date:** 2026-09-10
- **Applies to:** the golden-set generator (week 3), `scripts/leakage_probe.py`, `provenance/eval/schema.py`

## Context

Ground truth for this project is generated from the repository itself: find a merged PR that
closed an issue and modified a function; the PR body plus the issue discussion *is* the rationale
for that function's behaviour. This is the project's strongest asset — verifiable, ID-level gold
rather than an LLM judging an LLM.

It is also the project's largest single risk, because **an auto-generated benchmark can be aced by
a baseline that does none of the work the system exists to do.** If plain substring search scores
near-perfect recall, every row of the ablation table shows zero lift, and an examiner discovers it
by asking "did you try grep?"

We measured this two ways on the real corpus (apache/airflow, in-scope commit-message pool of
5,978 documents).

| Construction — where the query symbol comes from | grep recall@1 | grep recall@10 |
| --- | --- | --- |
| From the **gold document** (rarest backticked identifier in the PR body) | 0.897 | **0.986** |
| From the **code diff** (the function the commit actually modified) | 0.120 | **0.175** |

Median document frequency of the gold symbol under the second construction is **0**: most Airflow
function names appear in *no* commit message at all, including their own.

## Decision

The generator MUST source the anchor symbol from the **code** — the symbol whose body the commit
changed — and MUST NOT source it from the PR body, issue text, or any other document that is part
of the gold evidence.

Concretely, at generation time:

1. Extract the modified symbol from the diff, via `symbol_versions` / the AST, not by scanning
   prose for backticked tokens.
2. Compute and persist `gold_symbol_df` — how many documents in the pool contain the symbol.
3. Compute and persist `baseline_grep_r10` per query.
4. Discard any query that pure substring search answers at rank 1.
5. Assert the question shares no token of `df <= 5` with the gold document.

`scripts/leakage_probe.py` runs **both** constructions and reports them side by side. The gap
between them is the evidence that the generator is sound; a narrowing gap is the alarm.

## Consequences

**The leak is not caused by templating a question over a symbol.** It is caused by sourcing the
symbol from the text being retrieved. That distinction turns a vague instruction ("mask the
symbol") into a testable rule, and it is why the probe compares constructions rather than just
thresholding one number.

**A second-order consequence argues for the whole architecture.** Because most function names
appear in zero commit messages, lexical retrieval *structurally cannot* reach their rationale — no
amount of BM25 tuning helps, because the term is not in the document. Reaching it requires walking
`symbol -> blame -> commit -> PR -> issue`. Report the statistic "for N% of in-scope functions, the
rationale document does not contain the function's name" as motivation, measured on our own corpus
rather than asserted.

**Cost.** Filter 4 removes roughly the queries a naive system would have got right, so headline
recall numbers will look *lower* than an unfiltered set would produce. That is the point: the
retained set is the one where retrieval quality is actually observable. Report both counts, and
never quote a number from an unfiltered set.

## Alternatives considered

- **Threshold-only guard** (assert mean grep recall@10 < 0.35 and stop). Rejected as insufficient
  alone: it detects the symptom without naming the cause, so a future generator change can
  reintroduce the leak and the threshold becomes a mystery to satisfy.
- **Paraphrasing the question with an LLM to hide the symbol.** Rejected: it makes the benchmark
  depend on a model, is not reproducible across model versions, and can silently change what the
  question is asking.

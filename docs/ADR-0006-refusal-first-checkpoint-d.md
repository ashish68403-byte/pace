# ADR-0006: Checkpoint D, refusal first — what D builds, and the questions it must refuse

- **Status:** Accepted (2026-11-09) — committed with slice D1's spec, before any of D's code
- **Date:** 2026-11-09
- **Implements:** [`PREREGISTRATION.md`](PREREGISTRATION.md) claim 5; review findings 14, 15 and 17
  ([tracker 000](tracker/000_REVIEW_REMEDIATION.md)); [tracker 007](tracker/007_AGENT_REFUSAL.md)
  tasks 1 to 4
- **Refines:** [ROADMAP](ROADMAP.md) §5 checkpoint D and §6
- **Applies to:** slice D1, the unanswerable questions; D2, the served pipeline; D3, claim 5's run

## Context

**Checkpoint D as ROADMAP §5 listed it:** HEAD code chunks (moved there when C was designed), the
warm-resident reranker (finding 17), a five-tool multi-turn agent with enforced budgets, grounding
verification and refusal, per-query score logging from the first agent run (finding 15), and the
demo. Its gate: refusal precision and recall with Wilson intervals, and the risk–coverage curve
against two trivial abstainers.

**What D starts from (measured 2026-11-09):**

- **The time.** D is due on 13 Dec, the same day as the report's complete draft, at under 8 hours
  a week. Checkpoint C took four slices; D as listed is about twice that.
- **The questions.** All 7,199 retained questions are answerable: 4,206 on dev (3,135
  archaeology, 1,071 provenance) and 2,993 on test. They use two templates, "Why was `X` added to
  `path`?" and "Why did `X` change in <Month YYYY>?". ADR-0003 deferred unanswerable questions to
  D, and `UnanswerableStrategy` names five kinds, S1 to S5; nothing generates them.
- **The code.** No agent or LLM code exists. `provenance/api/main.py` is a traced skeleton whose
  stages are stubs. Retrieval has BM25, dense, RRF and a router, and no reranker, by design, until
  a warm process exists.
- **The data's rules.** DATA-PROTECTION.md §6 allows corpus text, already public on GitHub, to go
  to an LLM provider in the generation half only, never in the deterministic retrieval gate.
- **The corpus.** Pinned at `1e2ad803f7`, committed on 10 Sep 2026. Lineage has seen 5,073
  qualified names across the 303 files' histories.

## Decision

### 1. Refusal first, in three slices

D builds what claim 5 and its gate need. Each slice has its own spec, plan and branch:

- **D1, the unanswerable questions** (§2).
- **D2, the served pipeline.** One warm process retrieves, reranks with MiniLM, asks an LLM for an
  answer that cites its evidence or declines, verifies the answer's grounding, and logs every
  query's scores from its first call. `pace ask` and the eval runner both go through it, with hard
  budget caps and recorded replays (findings 15 and 17). It hides an S3 question's documents (§2).
- **D3, claim 5.** An addendum to this record fixes the operating point on dev before D's first
  run, with an amendment to the pre-registration saying the same. Then one run: refusal precision
  and recall with Wilson intervals, the risk–coverage curve, and claim 5's verdict.

Moved to future work, with their designs kept: the five-tool multi-turn agent loop ([tracker
007](tracker/007_AGENT_REFUSAL.md) task 5) and HEAD code chunks with their `chunk_evidence`
([tracker 002](tracker/002_CHUNKING.md)). Claim 5 is measured on D2's single-pass answerer. The
demo is `pace ask` through the same warm process.

### 2. The unanswerable questions (slice D1)

Every negative starts from a retained question chosen by salted hash, keeps its split, and keeps
its template and month where the kind allows, so the negatives follow the answerables' wording:

| Kind | Built from its source question | Dev / test |
|---|---|---|
| **S3, held out** | The question unchanged; its commit and pull request become the record's `held_out` evidence | 300 / 214 |
| **S4, after the pin** | Its function, asked "Why did `X` change in <October, November or December 2026>?" | 300 / 214 |
| **S5, nonexistent** | Its file, template and month, with a function name spliced from two real names in that file, a method keeping its class | 300 / 214 |

- **The share.** 300 of 4,206 dev and 214 of 2,993 test is one question in fourteen in each
  split. S3's sources are held back: D3's answerable sample is drawn from the other retained
  questions. 300 per kind puts each kind's refusal recall within about ±5 points (finding 14's
  n ≈ 40 gave about ±15).
- **The S5 check.** A spliced name is used only if lineage has never seen it and no rationale
  document contains it, ignoring case.
- **The hiding contract, which D2 implements.** For an S3 question, retrieval returns no pool
  document linked to its held-out commit or pull request.
- **Not built.** S1, because under these templates a change made only by a formatting commit
  still has an answer ("it was reformatted"). S2, because it needs hand-written questions.
- **The limits, stated now.** An S3 question may still be partly answerable from documents that
  are not its own, such as a later commit or another pull request, so its measured refusal recall
  is a lower bound. S4 and S5 can be refused by a date or a name lookup, which is why every result
  is reported per kind.
- **The answerable benchmark is untouched.** The negatives live in their own file; the golden set
  whose hash C2 to C4 recorded does not change, and C4's committed results stand.

### 3. Claim 5's comparisons (for slice D3)

Claim 5 is decided against the pre-registration's two trivial abstainers: always answering, and
abstaining at random at the same coverage. Tracker 007 task 2 named a fused-score threshold
instead. That threshold is the null hypothesis finding 15 asks the verifier to beat, so D3
reports it beside the decision, labelled as not pre-registered.

## Consequences

- D is about C's size, and the thesis's second contribution, refusal, is measured rather than cut.
- The multi-turn agent is not built. If D slips further, ROADMAP §6's first cut still applies:
  refusal becomes the future-work chapter, its design in tracker 007.
- 514 retained questions are held back as S3 negatives, and D's answerable sample comes from the
  rest.

## Alternatives considered

- **The full D.** About twice checkpoint C's work in five weeks of under 8 hours, with the report
  due the same day: the likeliest to end half-built.
- **Cutting D.** Safest for the grade, but claim 5 and refusal would go unmeasured.
- **S3 from the candidates the leakage filter dropped.** Disjoint from the answerables by
  construction, but those questions share rare tokens with their gold documents, so the negatives
  would differ from the answerables in wording as well as in evidence: a confound for claim 5.
- **Negatives inside `auto_full.jsonl`.** One file, but it changes the hash that C2 to C4's results
  record.
- **Hand-written negatives.** Realistic, but slow and small: the n ≈ 40 finding 14 criticises.

## References

- [`PREREGISTRATION.md`](PREREGISTRATION.md) claim 5; [ADR-0003](ADR-0003-rationale-pool-benchmark.md),
  Alternatives, on deferring unanswerable questions to D.
- Review findings 14, 15 and 17, [tracker 000](tracker/000_REVIEW_REMEDIATION.md); [tracker
  007](tracker/007_AGENT_REFUSAL.md).

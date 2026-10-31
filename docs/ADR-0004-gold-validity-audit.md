# ADR-0004: How much of the benchmark's gold explains its function

- **Status:** Accepted (2026-10-31) — committed before the sample is drawn and before any label
  exists
- **Date:** 2026-10-31
- **Refines:** [ADR-0003](ADR-0003-rationale-pool-benchmark.md) §4, which makes a record's gold
  every pool document of its change, and never measures what that text says
- **Closes:** review finding 6 ([tracker 000](tracker/000_REVIEW_REMEDIATION.md)), once the
  self-agreement pass is in
- **Applies to:** `provenance/eval/gold_validity.py`, `provenance/eval/labelling.py`,
  `provenance/analysis/linkage_bias.py`, `pace audit`, and every lift reported from slice B5 on

## Context

ADR-0003 generates a question from every function a qualifying commit added or changed. Its gold
is every pool document of that change: the commit message, and, when slice B2 linked the commit to
a PR, the PR's description and its human review and conversation comments (slice B4). The
project's premise is that this text holds the *why*. Nothing measures it. Review finding 6:

> nothing anywhere establishes that a retained record's gold document actually contains the
> rationale for the anchored function.

**Why it matters.** Suppose a fraction *e* of records have gold that holds no rationale. A system
that finds rationale can then be credited for at most 1 − *e* of them, and a true lift of *L*
points reads as about (1 − *e*)·*L*. At *e* = 30%, a 10-point lift reads as 7, close to the
4–6-point noise floor of finding 7. Both existing guards protect against gold that is too easy to
find: ADR-0003 §5's retention filters and §6's gate. Neither protects against gold that is wrong.
It is the first question a viva asks of a generated benchmark.

**The dev set this ADR samples** (the golden set regenerated on the grown pool,
`results/golden_census.json`, 2026-10-31) holds 4,206 retained records:

- 1,034 provenance records with a linked PR, and 37 without;
- 3,083 archaeology records with a linked PR, and 52 without.

A PR carries a median of 4 human comments (90th percentile 25, 99th 93, maximum 277).

## Decision

### 1. What is measured

A record's gold is **valid** when its text, read as an answer to the record's question, gives the
reason the function was added or changed. The **validity rate** is the share of dev records whose
gold is valid, estimated from a labelled sample with its 95% interval. The shares labelled
`mentions` and `unrelated` (§3) are reported beside it, so a reader can apply a more lenient
reading.

### 2. The sample, drawn once and committed before any label

- **Population:** the 4,206 retained dev records. Test is not opened (ADR-0003 §7).
- **Strata:** query class × whether the record has a linked PR.
- **Allocation, 56 records:**

  | Stratum | Records | Sampled |
  |---|---|---|
  | provenance, PR-linked | 1,034 | 11 |
  | archaeology, PR-linked | 3,083 | 33 |
  | provenance, commit message only | 37 | 6 |
  | archaeology, commit message only | 52 | 6 |

  The two commit-only strata get 6 records each, enough to see a large difference. The PR-linked
  strata share the other 44 in proportion to their size, so the PR-linked part of the sample
  weights itself. Equal allocation (14 per stratum) was considered and rejected: it gives 2% of
  the population half the sample, and widens the overall interval from about ±13 to ±19 points at
  a rate of 0.7.
- **Which records.** Within each stratum, the records with the smallest
  sha256(`gold-validity-v1:<qid>`), read as an integer. This is the rule ADR-0003 §7 uses for the
  split, so the draw depends only on the seed and the qid.
- **Presentation order:** ascending sha256(`gold-validity-order-v1:<qid>`) across all strata, so
  no stratum is labelled as one block.
- **The re-label subset:** the 15 sampled records with the smallest
  sha256(`gold-validity-relabel-v1:<qid>`). They are presented in the order of
  sha256(`gold-validity-relabel-order-v1:<qid>`).
- **Pre-registration.** `pace audit sample` writes the sample, with the pool version and a hash of
  each sampled record, and refuses to overwrite it. The sample is committed before the first
  label.

### 3. The codebook

Each label reads the text shown as an answer to the record's question. §4 says which text.

- **`rationale`.** The text gives a reason for this function's change: a bug it fixes, a need it
  serves, a design choice, or a requirement or decision it implements. A reason stated for the
  whole commit or PR counts when this function's change is plausibly part of what it explains.
  The labelling tool shows the function's change on request, to check.
- **`mentions`.** The text says what changed in a way that covers this function: it names the
  function, or describes the change the function underwent. It does not say why.
- **`unrelated`.** The text neither explains nor describes this function's change. It is about
  other parts of the change, or too generic to cover anything ("fix", "wip", "misc").

**When torn between two labels, take the one that claims less:** `mentions` over `rationale`,
`unrelated` over `mentions`. A stricter examiner then has little room to push the validity rate
lower.

**The labels are the author's.** The tool never proposes a label, and no model is asked for one:
an LLM's judgement of rationale would itself need this audit.

### 4. Two steps for a PR-linked record

- **First, the commit message alone:** the gold the record would have without slice B4.
- **Then, unless that label is `rationale`, the commit message and the PR together:** the PR
  description in full, and its comments on request. The second label can only claim at least as
  much as the first, because the text only grows. A first `rationale` therefore stands for both.
- **A commit-only record** gets one label, because its commit message is its whole gold.

The pair measures, on the same records, how much rationale PR text adds. That is the case for
slice B4.

### 5. Estimators

- **Per stratum:** the share labelled `rationale`, with its Wilson 95% interval. Each stratum is a
  simple random sample.
- **Overall, for the dev population:** the stratified estimator p = Σ W_h·p_h (Cochran 1977). W_h is
  stratum *h*'s share of the 4,206 records, so the over-sampled commit-only strata count for their
  2.1% of the population and no more.
  - The interval is a Wilson interval at Kish's effective sample size, n_eff = p(1 − p) / Var(p)
    (Kish 1965).
  - Var(p) = Σ W_h²·(1 − n_h/N_h)·p_h(1 − p_h) / (n_h − 1).
  - When every stratum's labels are unanimous, the variance is 0 and n_eff is the sample size.
- **What PR text adds:** the same estimator over the two PR-linked strata, once for the first
  label and once for the final one, and the number of records whose rationale only the PR text
  supplied.

### 6. Self-agreement, not inter-annotator agreement

A solo project cannot report agreement between annotators, and says so.

- **The second pass.** The author labels the 15-record subset again, through the same two steps,
  **at least seven days** after the last of their first labels, without seeing them.
- **What is reported:** unweighted Cohen's kappa on the final label, with observed agreement
  beside it (kappa misleads when one label dominates), and the same pair for the first label.
- **Its weight.** With n = 15 the kappa is indicative, not precise. The review asked for 15.
- **What is enforced.** The tool enforces the seven days and never displays a first label.
  "Without seeing them" is otherwise a matter of procedure, because the first labels are
  committed in the repository.

### 7. Two companions that need no labeller

**Reachability.** For every retained dev record: does any lexical baseline put a gold document in
its top 100? The baselines are grep, default BM25 and identifier-aware BM25. A record none of them
reach, at ten times the gate's *k*, is beyond every cheap signal, which bounds what their
recall@10 can mean. Dense retrieval joins when frozen vectors exist; until then the report says it
is not measured.

**Linkage bias** (Bird et al. 2009). The review warned of a benchmark built only from commits with
a recoverable PR link. ADR-0003 avoids that by generating questions from every qualifying commit,
linked or not. What remains is whether the commits B2 could not link to a PR — whose gold is a
commit message alone — differ from those it could.

- **The comparison.** Among qualifying pool commits (not merges, not mechanical), B2's `linked`
  outcome against `no_reference`, a commit that names no PR. The two `dropped` outcomes come from
  B2's numbering rules and are a separate matter.
- **Module:** the first directory under `airflow-core/src/airflow/` of each in-scope file the
  commit touched, by the file's path today; a file directly in that directory counts as
  `(top level)`. Each commit spreads a weight of 1 equally over the modules it touched, so a large
  commit counts once. The path at the commit would confound module with date, because of the 2025
  move into `airflow-core/`.
- **Author:** distinct authors in each group; the share of each group's commits made by its ten
  most frequent authors; and how many of those ten are also among the other group's ten. No name
  appears in the report.
- **Year:** commits per year of `committed_at`, in UTC.

Module and year each get a total-variation distance. The comparison is descriptive: the review
asked for a characterisation, not a test.

### 8. Every lift is quoted twice

From slice B5 on, every lift on the dev set is quoted raw and divided by the validity rate, at its
point estimate and at both ends of its interval. **The raw lift is the claim.** The divided one
shows how much a benchmark with partly empty gold may hide. It is not the more conservative
number: a lower validity makes it larger.

### 9. The audit describes one golden set and one pool

The sample records the pool version (`rationale-v1:commit=…`), and for each sampled record a hash
of its question, split, class, evidence and anchor. `pace audit label`, `relabel` and `report`
refuse once either differs from what is stored now. A regenerated golden set or a grown pool needs
a new sample. The old sample, its labels and its report stay in the history.

## Consequences

- **Finding 6 closes in three stages:**
  1. the tools, and the committed sample;
  2. the 56 labels and the report;
  3. at least a week later, the re-label and the kappa.

  B5's ablation rows are quoted only after the second stage.
- **The report is reproducible.** `pace audit report` regenerates
  `results/gold_validity_audit.json` from committed files and the database, byte for byte.
- **The labels are public.** The per-record labels and the labeller's notes are committed, so
  anyone can label the same records and compare.

### Known limits, stated rather than hidden

- **The labeller built the benchmark,** and would like it to be valid. The tie rule, the committed
  sample, the published labels and the self-agreement pass are the mitigations. None of them
  removes the conflict.
- **Six records per commit-only stratum** give intervals of about ±30 points: enough to see a
  large gap, not a small one.
- **The first label frames the second.** Reading the commit message first may colour how the PR
  text is read. The order stays, because it is the question being asked: what PR text adds to a
  commit message.
- **Blindness is procedural.** The first labels are in the repository.

## Alternatives considered

- **A simple random sample of 56.** It weights itself and gives about ±12 points, but it expects
  about one commit-only record, so it can say nothing about commit messages alone.
- **Equal allocation, 14 per stratum.** It gives the strongest comparison between PR-linked and
  commit-only records, but ±19 points on the number B5 divides by. Rejected for that.
- **An LLM as labeller.** Faster and repeatable, but its judgement of rationale is the claim under
  audit.
- **Labelling every record.** 4,206 records is weeks of work; 56 give about ±13 points.
- **Weighted kappa,** because the labels are ordered. Rejected for the standard unweighted kappa
  beside observed agreement. At n = 15 the choice changes little, and unweighted is what a reader
  expects.

## References

- Bird, C., Bachmann, A., Aune, E., Duffy, J., Bernstein, A., Filkov, V. and Devanbu, P. (2009).
  Fair and balanced? Bias in bug-fix datasets. *Proceedings of ESEC/FSE 2009*.
- Cochran, W. G. (1977). *Sampling Techniques*, 3rd edition. Wiley.
- Cohen, J. (1960). A coefficient of agreement for nominal scales. *Educational and Psychological
  Measurement* 20(1), 37–46.
- Kish, L. (1965). *Survey Sampling*. Wiley.
- Wilson, E. B. (1927). Probable inference, the law of succession, and statistical inference.
  *Journal of the American Statistical Association* 22(158), 209–212.

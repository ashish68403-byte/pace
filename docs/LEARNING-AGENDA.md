# Learning Agenda

Generated 2026-10-06 from a seven-lens review of [ROADMAP.md](ROADMAP.md). Companion to
[tracker/000_REVIEW_REMEDIATION.md](tracker/000_REVIEW_REMEDIATION.md), which tracks the
*fixes*; this file tracks the *understanding* each fix assumes.

---


Keyed to `docs/ROADMAP.md` §5. Written for the level of the roadmap's own prose: the concepts, not the tutorials.

**Budget.** Five self-tests, roughly one hour each. Five readings, roughly nine hours total. One resource per week block, one to two hours each. Call it 28 hours across 14 weeks — two hours a week, less than the time §5 already loses to a single mis-specified gate. If you do only three things, do self-test C1, self-test C3, and read Smucker, Allan & Carterette.

---

## 1. The five concepts that carry this project

Everything else in the roadmap is derivable from these. Each one is already load-bearing in committed code.

### C1. Minimum detectable effect for a paired ranking delta

**What it is.** The smallest true difference your comparison can distinguish from noise. For paired binary per-query outcomes it is governed by the **discordant-pair count** — the number of queries where the two arms disagree — and not by `n`, not by the net delta. Concordant queries cancel exactly out of the difference vector and contribute nothing to either the estimate or its variance. Write it out: if `d` queries flip down and `u` flip up, the paired delta is `(d-u)/n` and its standard error is about `sqrt(d+u)/n`, so significance needs

```
d - u  >  1.96 * sqrt(d + u)
```

in which `n` does not appear.

**Why this project lives or dies on it.** `provenance/eval/runner.py:89` sets `DEFAULT_THRESHOLD = 0.02` and `runner.py:93` sets `MIN_QUERIES_FOR_PAIRED_GATE = 100`. The roadmap defends the 2-point threshold from *determinism* — trap 5's "zero sampling noise, so a failing gate is always a real code change" — which is a correct argument about reproducibility and a non-argument about power. Determinism tells you the gate will not fire by accident. It says nothing about whether the gate will fire when it should. Those are different properties and the roadmap currently has only the first. The rule above says the gate needs roughly four net query flips before the paired interval excludes zero, independent of `n`; at `n = 100` that is a 4-point regression, twice the threshold, so the paired test and not the threshold is the binding constraint, and `DEFAULT_THRESHOLD` is decoration at that size. At `n = 500` the arithmetic inverts and the threshold binds. You cannot know which regime you are in without doing this.

**Sharpest resource.** Smucker, Allan & Carterette, *A Comparison of Statistical Significance Tests for Information Retrieval Evaluation*, CIKM 2007. It is where the paired-bootstrap-versus-t-test-versus-randomisation question was settled empirically, on exactly the kind of per-topic score table `per_query.jsonl` already holds. Read §3 and §5. As a ten-minute warm-up, write down McNemar's statistic first: it uses only the two discordant counts, which is the cleanest possible proof that concordant queries are free.

**Self-test, under an hour.** Twenty lines against `provenance/eval/metrics.py::paired_bootstrap`. Fix `n = 200`, draw a baseline vector of 0/1 recall@10 values, copy it to the system arm, flip `d` entries down and `u` up, and sweep. Record the smallest `d` at which `PairedCI.significant` turns `True` for `u = 0, 2, 5`. Then check the prediction `d - u > 1.96*sqrt(d+u)` against what the actual 10,000-resample percentile interval does. Repeat at `n = 100` and `n = 500`. The output is one sentence you can say in a viva: "my gate detects an X-point regression at n=Y, and here is the simulation." That sentence is a complete answer to the hardest question available about the CI gate.

---

### C2. The relevance unit is not the retrieval unit

**What it is.** Judgments are made about one kind of object and scores are computed over another. The metric is only well-defined when the denominator is fixed by the judgments and the retrieved list is collapsed into judgment-shaped units before scoring. Group judgments into units, score units, never score index entries.

**Why this project lives or dies on it.** You have already built the hard half and should know why: `GoldEvidence` sets `extra="forbid"` so a `chunk_id` cannot enter the gold set, and `resolve_anchors()` does the join at scoring time. The reasoning in `provenance/eval/README.md` is right — a content-addressed `chunk_id` changes when you re-split a function, so chunk-keyed gold would zero out recall for every chunker experiment. What that buys you is the ability to say, of the week 9-10 ablation table, "this measures retrieval and not chunking," and to have it be true. The half you have not done is the inverse direction: when one gold symbol resolves to three chunks and all three land in the top 10, `recall_at_k` must not count that as three hits out of one required item, and when a re-chunk turns one chunk into two the per-query value must not move. Those are two separate invariants and neither is currently tested; `README.md` notes that `chunk_evidence` is not exercised by CI at all yet.

**Sharpest resource.** Craswell, Mitra, Yilmaz, Campos & Voorhees, *Overview of the TREC 2019 Deep Learning Track* — specifically the section deriving document-level qrels from passage-level judgments by max-pooling over a document's passages, and the explanation of why the passage and document tracks are published separately rather than scored against each other. That is your `symbol -> chunks` fan-out with twenty years of qrels mechanics behind it.

**Self-test, under an hour.** Runnable today, before ingestion. Take twenty in-scope Airflow functions, chunk each one twice through `provenance.parse.chunker` at `max_tokens=400` and `max_tokens=250`, and assert that the set of `(path, qualified_name)` anchors is identical across the two runs while the `chunk_id` sets differ. Then write the two-line property test that fails if a future `recall_at_k` ever derives its denominator from the retrieved list rather than from `required`.

---

### C3. Selection on the dependent variable

**What it is.** Conditioning your test set on the outcome of one of the systems you are comparing. The resulting delta is not an estimate of the population delta; it is an estimate of the delta on the subpopulation where the comparator failed, and the bias has a known direction and a computable magnitude.

**Why this project lives or dies on it.** ADR-0002 filter 4 discards every query that pure substring search answers at rank 1. The lexical baseline is also the arm the ablation table is measured against. So the retained set is, by construction, the set where the baseline loses — and the ADR says so plainly in its Consequences section ("filter 4 removes roughly the queries a naive system would have got right"). The ADR treats this as a cost in absolute recall. It is more than that: it inflates the *delta*, which is the number the contribution rests on. There is no way to remove the filter without reintroducing the leak, so the answer is not mitigation, it is quantification — report the delta on the retained set and on the unfiltered set, with the retention rate, and own the gap. The examiner gets to pick the correction factor if you have not.

**Sharpest resource.** Heckman, *Sample Selection Bias as a Specification Error*, Econometrica 47(1), 1979 — §1 is enough, and it is where the "selection is a specification error, not a sampling nuisance" framing comes from. Be warned that the simulation below will teach you the effect faster than the paper will; read it afterwards for the vocabulary.

**Self-test, under an hour.** Fifteen lines of numpy. Generate 500 synthetic queries each with a latent difficulty, score two fixed systems against them (one lexical-only, one hybrid), compute the paired delta, then apply the rank-1 filter and recompute. Sweep the filter strictness and plot delta against retention rate. You will own the effect size for your own benchmark before you have built it, and that plot is a viva slide that converts the attack into a controlled measurement.

---

### C4. Gold validity: noise attenuates, and mined links are a biased sample

**What it is.** Two distinct properties of a derived benchmark. *Incompleteness and error* in the labels shrink measured effects multiplicatively. *Selection* in which items got labelled at all shifts what population the measurement describes. They are not the same problem and they have different fixes.

**Why this project lives or dies on it.** §1 calls derivable ground truth "the genuinely good part," and it is — it is also the only assumption in the design that is never validated. Work the attenuation out for your own case, because the shape is specific and worse than generic label noise: if a fraction `e` of gold items are wrong or unreachable, no arm can win those queries, so they are concordant at zero. They dilute the delta to `(1-e) * delta_true` **and** inflate `n` without inflating the discordant count. That means bad gold makes the gate look better powered while making it worse, which is how C1 and C4 compound into one problem. At `e = 0.3` a true 10-point contribution reads as 7 and drops under the gate's own noise floor. Separately, §3 reports 94% commit→PR linkage as evidence of sufficiency; linkage rate is a statement about the subset's *size*, not its *resemblance*. Bird et al. showed the linked subset is systematically different, not merely smaller.

**Sharpest resource.** Bird et al., *Fair and Balanced? Bias in Bug-Fix Datasets*, ESEC/FSE 2009. Short, directly about the artefact you are mining, and its method is reproducible on your corpus in an afternoon.

**Self-test, under an hour.** Runnable from the existing clone, no ingestion needed. One `git log` pass over the in-scope pathspec union, partitioned by whether the message yields a PR number, comparing three distributions across the two partitions: module (top-level package under `airflow-core/src/airflow`), author, and commit year. Write down *before you look at the output* what difference would force you to stratify the golden set. Deciding the threshold before seeing the number is the same discipline as writing the CI gate before the system, which you have already done once.

---

### C5. Gate liveness: the difference between "the invariant held" and "the invariant was not exercised"

**What it is.** A threshold test on a quantity that was never computed passes vacuously. Every gate therefore needs two assertions: the threshold, and a liveness check proving the gate ran against real input.

**Why this project lives or dies on it.** Four instances are live in the repo right now, with the same shape. `artifacts/leakage_probe.json` currently reads `n_retained: 0`, `grep_recall_at_10: 0.0` — which sails under the `< 0.35` ceiling, and the probe gets this right by inventing a third verdict, `INCONCLUSIVE`. Nothing else does. `ScopeDriftError` in `provenance/ingest/scope.py` cannot fire until `scope.txt` is committed. §3 asserts the corpus excludes `api_fastapi/` and calls the nine decisions "all now settled and in the code"; `DEFAULT_EXCLUDE` at `scope.py:57` contains `ui`, `example_dags`, `migrations`, `__pycache__` and `*.pyi`, and **does not contain `api_fastapi`** — a prose claim about code that no test checks, silently admitting the 36.5k lines §3 says are out of scope. The README already names this ("a check that cannot fail is not a check") and fixed it for the invariants' exception classifier. The general concept is what lets you find the next one without being told.

**Sharpest resource.** Papadakis, Kintis, Zhang, Jia, Le Traon & Harman, *Mutation Testing Advances: An Analysis and Survey*, Advances in Computers 112, 2019 — read the sections on test adequacy and on whether mutants represent real faults. Mutation testing is the systematic form of the question "would this test have failed if the code were wrong?", and the survey is the shortest route to the vocabulary without installing anything.

**Self-test, under an hour.** For each of the four ringed weeks in §5, write down the exact number a *not-run* gate would emit, and whether the gate can tell that apart from a pass. Then add the three liveness assertions that fall out: `n_retained > 0` before the grep ceiling is applied, `scope.txt` exists and is non-empty before a scope comparison is claimed, and a single constant holding the four excluded subtrees that both `DEFAULT_EXCLUDE` and a test over the §3 table read from. The last one is three lines and closes a defect that currently inflates your corpus by 38% of its lines.

---

## 2. Week by week

One concept per block, to be understood *before* the block starts, not during it.

### Week 3 — Golden-set generator; naive baseline

**Concept.** Your judgment pool is radically incomplete — one or two judged documents against a pool of 5,978 — and retention filtering changes the estimand, so decide what the retained-set delta is an estimate *of* before you write the filter (C3). **Without it:** you report a lift conditioned on grep failing, with no unfiltered comparison, and the number is unfalsifiable in the direction that flatters you.

**Resource.** Buckley & Voorhees, *Retrieval Evaluation with Incomplete Information*, SIGIR 2004 — what happens to measured scores when judgments are incomplete, and why bpref and condensed lists exist.

**Scheduling note, raise it before you start.** Week 3 needs the anchor extracted from the diff via `symbol_versions` or the AST (ADR-0002, step 1), but the symbol layer is weeks 6-7 and ingestion is 4-5. §2 calls moving the generator to week 3 "the single most important reordering" in the plan, and as scheduled it depends on three later weeks. Either week 3 produces a provisional generator over a regex-extracted anchor and is explicitly re-run after week 7, or the week table is wrong. Decide which, in writing, before week 3.

### Weeks 4-5 — Real ingestion: git walk, GitHub backfill

**Concept.** Content addressing gives you idempotency only if nothing non-deterministic reaches the hash input — grammar version, dict iteration order, absolute paths, locale-dependent normalisation — and "zero net writes on run 2" is equally satisfied by writing nothing at all (C5). Second: request amplification. `RUNBOOK.md` §3 already names N+1 as the characteristic bug of `pace.graph.expand`; the backfill is the same shape one layer up, where a paginated list response already contained the field a per-item request is about to buy back. **Without it:** the idempotency gate passes vacuously on an empty ingest, and the backfill takes 29 hours instead of 25 minutes.

**Resource.** GitHub REST *Best practices for integrators* together with the primary and secondary rate-limit documentation. Then instrument `scripts/fetch_corpus.py` with a request counter and print requests-per-record at the end of a `--limit 200` run; a counter reading 1.0 where it should read 0.01 is the bug, visible in one line, and the number is quotable in §8 next to the $25.

### Weeks 6-7 — Chunking, symbols, blame

**Concept.** A lexical token count is not a model token count. `MAX_CHUNK_TOKENS = 400` is justified as "sized for bge-small (512 tokens)", but `estimate_tokens` (`chunker.py:125`) is a regex count that scores `compute_something_long_12` as one token where bge-small's WordPiece scores roughly seven; subword fertility on identifier-dense code is the gap between a chunk that embeds and a chunk that is silently truncated. Second, and it is the gate itself: the week 6-7 target is ≥0.9 commit-set F1 against `git log -L`, and §7's own Flask example puts `git log -L` at about 62% precision, so that gate measures *agreement with a noisy reference*, not accuracy. **Without it:** you truncate the tail of your longest chunks without knowing, and you write "accuracy" in a thesis chapter where only "agreement" is true.

**Resource.** The tokenizer section of Li et al., *StarCoder: may the source be with you!* (2023) — it motivates a code-specific vocabulary precisely by identifier fertility. Then measure your own: tokenize 200 chunks with `AutoTokenizer.from_pretrained("BAAI/bge-small-en-v1.5")` and plot WordPiece count against `estimate_tokens`. The ratio is one number you can cite.

### Week 8 (ringed) — Function identity, minimum viable version

**Concept.** Dose matching. "Lineage on versus blame-only" varies the mechanism *and* the quantity of evidence simultaneously, and the roadmap's own numbers put the dose difference at 4.0x corpus-wide (5,978 versus 1,479 commits) and 6.4x on `models/dag.py` (611 versus 95) — almost certainly larger than any mechanism effect you will observe. The fix is to make the evidence budget a parameter of the *metric* rather than a property of the arm, so an arm physically cannot be scored at a different budget. The cheap addition is a placebo arm: edges rewired to random same-era commits with the degree distribution preserved, which is the difference between "lineage helps" and "these specific edges help." **Without it:** the headline ablation row measures how much evidence each arm was allowed to see.

**Resource.** Da Costa et al., *A Framework for Evaluating the Results of the SZZ Algorithm for Identifying Bug-Introducing Changes*, IEEE TSE 43(7), 2017. It is about how the field evaluates a blame-correction, which is precisely the design problem of your week-8 ablation. RA-SZZ is the closest published relative of your intervention — same intervention, new dependent variable — and after reading it you should be able to answer in three sentences: *what does my ablation measure that RA-SZZ's evaluation does not?* If you cannot, the novelty claim in §7 is not yet precise enough.

### Weeks 9-10 — Hybrid retrieval and the ablation table

**Concept.** Grouped splitting and the winner's curse. Your gold items cluster by PR and by commit, so a random split leaks — the same PR body is the gold document for several queries and will land on both sides of the boundary. And with 20-50 configuration choices selected on one set of a few hundred queries, the expected inflation of the best observed configuration is the same order of magnitude as the lift you are claiming, even when every option is truly identical. **Without it:** your tuned configuration's reported advantage is partly the maximum of a noise distribution, and the ablation table attributes it to a stage.

**Resource.** Dodge, Gururangan, Card, Schwartz & Smith, *Show Your Work: Improved Reporting of Experimental Results*, EMNLP 2019 — it reformulates reported performance as a function of search budget. Pair it with the scikit-learn documentation for `GroupShuffleSplit` and `StratifiedGroupKFold`, specifically on why `groups` exists; that parameter is your PR-number leak. Then reproduce the winner's curse in a ten-line loop: watching identical systems produce a six-point "winner" is more convincing than reading that they do.

### Weeks 11-12 — Agent loop: five tools, budgets, state machine

**Concept.** Warm residency is a process topology, not a configuration flag. §2 asserts the reranker "must be a warm-resident process" and measured 97% of the first real trace as cold model load; whatever holds the weights must be the only thing that holds them, and anything that reports a latency number must go through it. This is trap 6 one layer up — if the eval runner loads the model in-process and serving goes through a worker, your two headline numbers again describe two different systems. **Without it:** you report a p95 that no served request can achieve, and the gate guards a process topology that does not exist in production.

**Resource.** The ONNX Runtime performance documentation on `InferenceSession` creation cost and session reuse, read alongside gunicorn's design notes on the prefork model and `--preload`. The pattern to internalise is fork-after-load, and the design rule is that your eval runner should be a *client* of the serving process rather than a second loader.

### Week 13 (ringed) — Grounding verification and refusal

**Concept.** Precision is prevalence-dependent; recall is not. Write precision in terms of recall, false-positive rate and prevalence and watch the prevalence term appear — it is the base-rate identity from diagnostic testing. Consequence: refusal precision moves when you change the unanswerable base rate, so you cannot oversample negatives to stabilise it. Measure refusal recall and the false-refusal rate on answerables, which do not move, and *derive* precision. Second: §5's gate is two proportions on roughly 40 cases, where a percentile bootstrap is noticeably off at `p` near 0.9. **Without it:** your refusal numbers are a property of your test-set composition, quoted as a property of your system, with intervals your own `metrics.py` docstring would reject.

**Resource.** El-Yaniv & Wiener, *On the Foundations of Noise-free Selective Classification*, JMLR 11, 2010 — the risk-coverage formulation is the established framing for exactly the claim you are making, and it supplies the comparison §5 lacks: a confidence-threshold abstainer as a baseline. The implementation is a sort and a cumulative mean over one score column, so the only hard part is deciding in week 11 to log that column. For the intervals, `statsmodels.stats.proportion.proportion_confint(method="wilson")` is one call.

### Week 14 — Hardening lite; the demo

**Concept.** The demo and the §7 claim are one deliverable and they fail the same way: by being unreproducible after a re-fetch, and by being refutable by a single paper. Every number on a slide needs a corpus SHA and one command behind it — §4's 4,008 / 17,015 / 303 / 81,619 already meet that bar, while 5,978, 1,479 and 0.986 do not, because "unioning the pre- and post-move pathspecs" does not pin down the per-file union of 606 pathspecs that actually produced it. The moment the clone is re-fetched you lose the ability to distinguish a method difference from upstream drift. On the claim side, replace the superlative in §7 ("the first measurement of") with the 2x2 positioning table over intervention and dependent variable; the table, not the sentence, is what survives a counterexample. **Without it:** one paper refutes one sentence and the burden then flips onto every other claim in the document.

**Resource.** The ACM *Artifact Review and Badging* criteria (v1.1) — short, and it is the checklist an examiner applies informally. Then `man gitglossary` on pathspecs, specifically `:(exclude)` and the fact that `**/*.py` in a git pathspec does not behave like a shell glob: it misses files sitting directly in the named directory. The practical deliverable is one `scripts/corpus_facts.py` emitting every §4 number plus `git rev-parse HEAD` of the corpus into a committed JSON, with §4 citing that file instead of inline literals.

---

## 3. Things you will believe that are wrong

### On IR evaluation

**"recall@10 and MRR@10 are two views of the same quality."** They see disjoint changes. On a query with one required item, recall@10 is binary and cannot observe any reordering inside the top 10, while MRR sees nothing else. A reranker that lifts gold from rank 9 to rank 2 scores exactly zero on your headline metric. Decide which one the ablation table reports and say why; reporting both without saying which the gate guards is worse than reporting one.

**"recall@k behaves the same whether a query has one relevant document or five."** With one required item, recall@10 is success@10 — a hit rate — and its per-query value lives on `{0, 1}`, so the mean quantises to `1/n`. That is the argument in `provenance/eval/README.md` for why `n = 40` is incoherent, and it is right. The part not yet written down: your gold is *not* uniformly 1-relevant, because `GoldEvidence` carries required and supporting items, so a query with three required items has per-query values on a grid of thirds. Averaging queries whose values live on different grids means the step size of your headline number is set by the coarsest stratum — a single 1-required query flipping moves the mean by `1/n` regardless of how fine the rest are. Report the distribution of `|required|` next to every recall number.

**"A 95% bootstrap CI means a 95% chance the true value is inside it."** It is a coverage property of the procedure, and the percentile bootstrap's coverage degrades exactly where this project operates: small `n`, `p` near 0 or 1. At `n = 40` with `p = 0.9` it under-covers noticeably. Use Wilson. More importantly, note what a CI does not tell you: it bounds the *estimate*, not the *detectability*. An interval excluding zero says the sign is probably right; it says nothing about whether your gate would have caught a regression, which is a power question answered by C1 on the same data.

**"A non-significant paired interval means the two systems are equivalent."** It means the discordant-pair count was too small. With `n = 312` and eight queries differing, nothing is detectable and the result is uninformative, not negative.

**"Overlapping marginal CIs mean no difference."** Your README already kills this one. The inverse error is still live: a paired interval that excludes zero on `n = 312` while only eleven queries differ is an inference driven by eleven queries. Report the discordant count beside every paired delta, and the honest sentence writes itself.

**"Refusal precision is a property of my system."** It is a property of your system *and* the unanswerable base rate in your test set. Change the mix and it moves without a line of code changing.

**"The gate is deterministic, so the gate is sound."** Determinism rules out false alarms. Soundness also requires that the gate can fire — liveness (C5) — and that it fires for effects of the size you care about — power (C1). The roadmap argues the first of three.

### On git history analysis

**"`git blame` tells me who wrote a line, or why it is the way it is."** Blame computes, for each line of one revision, the most recent commit that last touched that line's text under whatever rename and copy heuristics were active. It is a terminal attribution, not a history, and not a cause. It cannot see a line that was reverted, reformatted, or moved as part of a block — which is exactly why `.git-blame-ignore-revs` exists, and why 29% of blamed lines in your corpus land on a mechanical commit while only 0.65% of in-scope commits are mechanically titled. The two numbers are consistent: thirty-nine commits touch a thousand files each.

**"Renames are recorded in the repository."** They are not. Git stores snapshots; every rename is *inferred at read time* from content similarity. So the answer depends on configuration: `-M`'s similarity threshold and `diff.renameLimit`, whose default is 1000. Your three mechanical commits touch 1,066, 1,090 and 1,136 files, all above that limit, so git's rename detection silently degrades on precisely the commits that matter most. A fact that changes when you change a config flag is not ground truth, and `-M -C` at 3,233 ms versus `-M` at 948 ms is the price of a better guess, not of a lookup.

**"`git log --follow` follows a file."** It follows one pathspec, is applied after history simplification so it can drop commits, does not work with multiple paths, and stops at the first rename it cannot resolve. That is the 95-versus-611 gap on `models/dag.py`, and it is also why a shallow clone is fatal: the graft makes `blame -M -C` stop dead at the boundary.

**"`git log -L` gives the history of a function."** It gives the history of a *line range* projected backwards. §7's Flask example is the whole lesson: 21 commits returned, eight of them pre-dating the function's introduction, and the 2023 commit that recreated the file omitted entirely. Since the week 6-7 gate measures against `git log -L`, the correct word in the thesis is "agreement", and the correct companion sentence names the reference's own precision.

**"94% commit→PR linkage means the linked subset is representative."** Linkage rate measures size. Representativeness is a separate claim requiring a separate measurement, and the measurement is one `git log` pass (C4).

**"A content-addressed id makes ingestion idempotent."** Only if the hash input is free of grammar versions, dict ordering, absolute paths and locale. And `ast.dump` is not stable across Python minors — which is why §7 already tells you to hash the tree-sitter s-expression and stamp the grammar version. Pair that constant with the `chunk_id_set_hash` invariant or a grammar upgrade reads as a retrieval regression.

---

## 4. Read these five things, in this order

1. **Smucker, Allan & Carterette, *A Comparison of Statistical Significance Tests for Information Retrieval Evaluation*, CIKM 2007.** Read before week 3: `DEFAULT_THRESHOLD` and `MIN_QUERIES_FOR_PAIRED_GATE` are already committed, and this is the paper that tells you whether they mean anything.
2. **Bird et al., *Fair and Balanced? Bias in Bug-Fix Datasets*, ESEC/FSE 2009.** Read before weeks 4-5: the backfill decides which commits become linkable, and the bias is cheapest to characterise while you are already walking the log.
3. **Śliwerski, Zimmermann & Zeller, *When Do Changes Induce Fixes?*, MSR 2005.** Read before week 8: six pages, and it is the original blame-walking algorithm whose naive attribution your week-8 arm is the correction of. It is how you position the claim as "same intervention, new dependent variable" rather than as a superlative.
4. **Grund, Chowdhury, Bradley, Hall & Holmes, *CodeShovel: Constructing Method-Level Source Code Histories*, ICSE 2021.** Read before week 8: it ships the Python parser and the 40-method, 327-entry oracle that §7 already plans to use as your evaluation set, and its failure analysis of `git log -L` is the justification for the week 6-7 gate's wording.
5. **Brown, Cai & DasGupta, *Interval Estimation for a Binomial Proportion*, Statistical Science 16(2), 2001.** Read before week 13: week 13's entire output is two proportions on about 40 cases, and this is why Wilson beats both the normal approximation and a naive bootstrap at small `n` and extreme `p`. It also fixes the 16/40 interval in §7, which is currently quoted without one.
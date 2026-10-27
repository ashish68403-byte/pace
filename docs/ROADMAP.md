# Roadmap

**Provenance-Aware Code Intelligence — re-scoped for one person, delivered in four checkpoints.**

The specification budgets 620–700 engineer-hours across two or three people over six months.
Realistic full scope is closer to 1,150–1,450. This plan assumes ~210–320 hours and one pair of
hands. Since 2026-10-27 it is ordered as four checkpoints (§5), each one a complete submission,
so that wherever time runs out the submission is still complete, measured and defensible.

---

## Status

| | |
|---|---|
| Last updated | 2026-10-27 |
| Current checkpoint | **A** — the history graph and the truncation measurement |
| Last completed | Phase 0 (`7951158`), scope freeze (`81f121b`) |
| Deadline | final submission mid-to-late December 2026 |

**Done.** Environment (WSL2 ext4, Postgres 18.6 + pgvector 0.8.4 + pg_search 0.25.7), corpus
cloned at `1e2ad803f7`. Scope frozen and committed: 303 files / 81,619 lines (`scope.txt`).
Schema: five Alembic revisions, 26 tables and one view (`provenance_edges`); a live `\dt` shows
28 because the database also holds PostGIS's `spatial_ref_sys` and `alembic_version`. Walking
skeleton: `pace demo` produces one trace in Jaeger. 117 tests, ruff clean.

**Next.** Checkpoint A ([tasks/ingest/](tasks/ingest/)): `pace ingest` writes commits, parent
links, files and file revisions; `pace measure truncation` measures how much history a
current-path view hides. Until it lands the graph is empty, and every evaluation number is
structurally zero.

---

## 1. What this project really is

A typed provenance graph over a repository's history, with a retrieval system on top that walks
edges — code → symbol → commit → PR → issue → review comment — instead of embedding text and
hoping.

The genuinely good part is not the RAG. It is that **ground truth is derivable from the repository
itself**: a merged PR that closed an issue and modified a function *is* the rationale for that
function, giving verifiable ID-level gold rather than an LLM judging an LLM. That, plus refusal on
rationale that was never written down, makes this an engineering-measurement project. Measurement
projects survive vivas.

The riskiest thing about it is the same thing: an auto-generated benchmark leaks. See trap 1 and
[ADR-0002](ADR-0002-golden-set-construction.md).

---

## 2. Verdict on scope

Doable at roughly half the spec's ambition. The cuts are not evenly distributed.

Phase 2 (chunking, symbols, blame, function identity) is the worst-costed part of the spec — four
separately hard problems budgeted as one, at about 2.6–3.2× under. Phases 3 and 4 (baseline and
retrieval) are the only well-estimated section. So this plan protects evaluation and retrieval,
spends its remaining risk budget on exactly one novel thing, and deletes the platform work.

**The single most important reordering:** the spec puts the naive baseline in phase 3 and the
golden set in month 5. Both move forward, to checkpoint B (§5) — ahead of function identity and
of every retrieval experiment. Not for schedule reasons — because until you have
measured what a plain substring search scores on your own benchmark, you do not know whether the
benchmark measures anything at all.

### Hardware is the binding constraint, not the schedule

Intel i3-1115G4, 2 physical cores, 7.7 GB RAM (WSL sees ~3.7 GB), no CUDA. Measured on this
machine:

| | |
|---|---|
| CPU embedding | ~4.7 chunks/s |
| `bge-reranker-base`, 50 candidates | **23.83 s** (against a 9 s p95 budget) |
| `ms-marco-MiniLM-L6-v2`, 50 candidates | 3.57 s |
| `ms-marco-MiniLM-L6-v2`, 10 candidates | 0.63 s |
| Cold model load | 126 s / 17.4 s respectively |

Confirmed on the first real trace: 97% of an 11.99 s demo query was cold model load. Whatever
reranker you pick **must be a warm-resident process**, and batch embedding belongs on Kaggle's free
GPU hours, not this laptop.

---

## 3. The nine decisions

Each is reversible in week 1 and expensive in week 8. All are now settled and in the code.

| Decision | Choice | Why |
|---|---|---|
| **Corpus** | Airflow, ~81.6k lines / 303 files | Not the spec's 200k. `airflow-core/src/airflow` minus `ui/`, `example_dags/`, `migrations/`, `api_fastapi/`. The excluded `api_fastapi/` is 30,640 physical lines across 177 files of REST boilerplate with short history and low rationale density — 27% of the tree by file count, 37% by lines, measured at corpus `1e2ad803f7` and reproducible with `pace scope`. Measured: 5,628 in-scope PR numbers, 94% commit→PR linkage, ~190–370 golden items after filters — enough. |
| **Python** | 3.12.3 (already in WSL) | The spec's exact version, zero install, matches `ubuntu-latest` in CI. |
| **Code location** | WSL2 ext4, never `/mnt/c` | Measured: `/mnt/c` costs 0.2–0.9 s per `git status`, ~190 ms per `git blame`. This project runs blame tens of thousands of times. |
| **Containers** | Docker already in WSL | Do *not* install Docker Desktop — it displaces a working engine and costs 1.5–2 GB on an 8 GB box. |
| **Vector store** | pgvector 0.8.4+, one datastore | No Qdrant. 0.8.3 fixed possible HNSW corruption during vacuum, 0.8.4 fixed unrepaired graphs. Do not drift below 0.8.4. |
| **Lexical** | ParadeDB `pg_search` | Real BM25 inside Postgres with a tokenizer that splits camelCase *and* snake_case. Fallback documented; note `ts_rank_cd` is **not** BM25. |
| **Symbols** | tree-sitter + Jedi, behind an interface | `scip-python` is effectively dead (last real commit 2025-09-05, absent from the `scip-code` org SCIP moved to). SCIP becomes a stretch goal and an ablation row, never a dependency. |
| **Reranker** | ms-marco-MiniLM-L6-v2, warm-resident | The spec's BGE-class reranker is 2.6× the entire latency budget on this CPU. |
| **LLM** | One hosted + one local | Satisfies the two-provider failover requirement, costs nothing, doubles as the no-API-key contingency. Pin the exact model string in the baseline JSON. |

---

## 4. Six traps, ranked

Each was measured against this corpus or this machine. Each one, unmitigated, invalidates a
headline number.

### Trap 1 — a naive golden set is aced by grep

Source the query symbol from the PR body and plain substring search scores **recall@10 = 0.986**
(n=358). The gold symbol has median document frequency of **1** — it is a primary key, not a query.
Every ablation row would then show ~0 lift, and an examiner finds it by asking "did you try grep?"

**The mechanism is specific.** Sourcing the symbol from the *code diff* instead gives recall@10 =
**0.175**, with median document frequency **0**. The leak is not caused by templating a question
over a symbol — it is caused by sourcing the symbol from the text being retrieved.

*Mitigation:* [ADR-0002](ADR-0002-golden-set-construction.md). Extract the anchor from code, persist
`baseline_grep_r10` and `gold_symbol_df` per query, discard anything grep answers at rank 1, and
assert mean grep recall@10 < 0.35 in CI. Run both constructions and report side by side.

*The upside:* because most Airflow function names appear in **no commit message at all**, lexical
retrieval structurally cannot reach their rationale. That is the sharpest argument for this whole
architecture, and it is measured on our own corpus rather than asserted.

### Trap 2 — a 2025 directory move truncates every in-scope history

Commit `243fe86d4b` (2025-03-21) moved `airflow/` to `airflow-core/src/airflow/`. Counted by path,
the scope has **4,008** commits reaching back only to March 2025; the legacy path holds **17,015**.
Per file: `models/dag.py` shows 95 commits by path and **611** with rename-following.

*Mitigation, and it is cheaper than expected:* the move was a pure prefix rename, so unioning the
pre- and post-move pathspecs recovers **5,978** in-scope commits versus 1,479 — 4×, with no
rename-chain reconstruction needed at this scope. Never shallow-clone: the graft makes
`blame -M -C` stop dead at the boundary, which is exactly the failure the novelty claim exists to
fix.

*Free bonus:* that commit is a hand-labelled 605-file ground-truth fixture for function identity
across moves. Make it the Phase-2 acceptance test.

### Trap 3 — mechanical commits poison the primary edge

Airflow ships a `.git-blame-ignore-revs` with **7** SHAs. Verified: Black `4e8f9cc8d0` touched
1,066 Python files, PEP-563 `d67ac5932d` touched 1,090, and `bfcae349b8` touched **1,136** — larger
than either commonly cited. Three of them exceed git's `diff.renameLimit` default of 1000, so git's
own rename detection silently degrades on exactly the commits that matter most.

Measured: **29% of blamed lines** land on a mechanical commit, but only **0.65% of in-scope commits**
(39 of 5,978) are mechanically titled. Not a contradiction — those few commits each touch a
thousand files. It means a subject-regex filter is cheap and high-yield.

*Mitigation:* blame with `--ignore-revs-file`, **including in the baseline**, or the comparison is a
straw man. Add `commit.is_mechanical`; report the recall improvement as an ablation row.

### Trap 4 — the symbol layer's prescribed tool is unmaintained

Covered in §3. The knock-on the spec does not name: **Cross-cutting is the query class most likely
to fail silently**, because it rests entirely on `find_references` with no stated fallback.

*Mitigation, a free upgrade:* reframe Cross-cutting as idiom detection over the normalised AST
hashes you are already building for function identity. "Where else is this retry-with-jitter pattern
used" is a copy-paste question, not a call-graph question — clustering answers it *better*.

### Trap 5 — an LLM in the CI gate

Read naively at frontier list rates, this costs ~**$19,400** over six months. Two corrections to the
obvious savings plan: the Batch API is single-shot and **cannot batch a multi-turn agent loop**
(batch the judge calls, not the loop), and prompt caching saves far less than assumed because the
token mass is per-query history and retrieved documents, which never cache across queries.

*Mitigation — the one insight that makes CI free:* **retrieval evaluation needs no LLM and no
embedding API.** Freeze query embeddings into a committed `.npz`, restore a small `pg_dump` index
into a pinned container, cache rerank scores. `recall@10` compares IDs to IDs — zero model calls,
zero sampling noise, so a failing gate is always a real code change. That is the only thing that
makes a 2-point threshold defensible. The LLM half runs in cassette replay, live only on a schedule.

### Trap 6 — benchmarking one system and serving another

If you benchmark on a borrowed GPU at depth 50 but serve on CPU at depth 20, your two headline
numbers come from two different systems and the CI gate guards a system that does not exist.

*Mitigation:* pick the deployment target explicitly, report the CPU-served configuration for both
numbers, and say which one the gate measures.

---

## 5. Four checkpoints

Re-cut on 2026-10-27 from the real position — Phase 0 done, the graph empty, a mid-December
deadline, under 8 hours a week of the author's time — after review finding 1 showed that the week
table could not run in its written order. Each checkpoint is a complete project that could be
submitted on its own; later ones add to it rather than finish it. Aim for D, submit the furthest
checkpoint reached, and the rest becomes the future-work chapter with its design already written.
Every gate states its liveness check, because a gate that passes on an empty input is not a gate.

| Checkpoint | Target | Adds | Gate | Submittable as |
|---|---|---|---|---|
| **A** | Sun 8 Nov | Commit graph, file history across renames, the truncation measurement, `pace history` ([tasks/ingest/001-003](tasks/ingest/)) | Run 2 inserts 0 rows **and** skips exactly run 1's inserts; the current-path check agrees with git on 303/303 files; `results/history_truncation.json` reproduces byte for byte | A rename-aware history graph of Airflow, and a census of how much history a current-path view hides |
| **B** | Sun 22 Nov | Ethics self-assessment dated before the first fetch (finding 11); GitHub backfill under a request budget (finding 10); HEAD chunks and `chunk_evidence`; the dev/test split (finding 2) and the generator's target `n` (finding 7); golden-set generator v0 — diff-anchored, AST-only anchors ([ADR-0002](ADR-0002-golden-set-construction.md)); leakage probe; grep and BM25 baselines, then dense retrieval and RRF — the ablation table's first rows | The leakage verdict is computed on `n_retained ≥ N`, with `N` fixed from finding 7's MDE before the generator runs, **and** grep recall@10 < 0.35. An empty retained set is INCONCLUSIVE, never a pass | A + a benchmark grep cannot ace, with honest baselines |
| **C** | Sun 6 Dec | Blame with `--ignore-revs-file`; function identity, minimum viable (§7: two hash tiers, same-file, one cross-file rule); `docs/PREREGISTRATION.md`; the three-arm lineage-vs-blame comparison (finding 3) | `PREREGISTRATION.md`'s commit precedes the first comparison run in `git log`; the volume-matched arm is reported beside the unbudgeted one | B + the novelty measurement: provenance-chain recall, lineage vs blame |
| **D** | Sun 13 Dec | The warm-resident reranker, so the evaluated and the served system are one process (finding 17); agent loop (five tools, enforced budgets); grounding verification and refusal; per-query score logging from the first agent run (finding 15); the demo | Refusal precision **and** recall with Wilson intervals; the risk-coverage curve against two trivial abstainers | The full system |
| **Report** | weekly from Mon 2 Nov | One chapter per checkpoint as it lands, methods first | A complete draft by Sun 13 Dec | — |
| **Buffer** | 14–20 Dec | The report, viva preparation | — | — |

**On "byte-identical index":** unachievable and should not be an automated test. HNSW build order,
Postgres physical state and float reductions all defeat it. Use instead: zero net writes on run 2,
identical chunk-ID set hash, empty dead-letter table, identical top-10 IDs under exact k-NN.
Saying in the report that you *rejected* byte-identity and why is the stronger viva answer.

---

## 6. What to cut, in cut order

When a checkpoint's date slips, cut from the top. Every item here is scheduled in §5, so cutting it
really does give time back. (The previous version of this list named thirteen things that had
never been scheduled, so it could not rescue anything — review finding 1. They are kept below as
what they always were: deliberate exclusions.)

1. **D's agent and refusal.** Their design (tracker 007) becomes the future-work chapter.
2. **C's cross-file identity rule.** Same-file identity still yields the comparison, on fewer events.
3. **C entirely.** B's benchmark becomes the evaluation; the lineage claim becomes future work,
   with its pre-registration already written.
4. **B's dense retrieval.** grep and BM25 are the baselines the leakage argument needs.
5. **B's GitHub backfill.** The benchmark falls back to commit messages as its rationale documents —
   weaker, still a measurement, and reported as such.

> **STOP — below here is the grade.** Checkpoint A, the report, and whichever benchmark exists
> together with its leakage guard. These are never cut.

### Out of scope from the start

Not schedule recovery — none of these was ever scheduled. Each is an exclusion with its reason.

1. **k3s** — the spec says it only pays for multi-node indexing. ~3 weeks.
2. **Qdrant** — one store. Name its filterable HNSW as the true pre-filter you didn't take.
3. **OpenSearch** — 2 GB of JVM on a 7.7 GB host for no scientific gain.
4. **Redis + RQ** — `SELECT … FOR UPDATE SKIP LOCKED` keeps job state in the same transaction as the data it produces, which *is* the idempotency story.
5. **GH Archive / BigQuery** — a full-history single-repo scan is 15–27 TB, and the payload no longer carries the PR fields needed since GitHub cut `PullRequestEvent` from 48 keys to 5 in Oct 2025. Run one narrow query in the free sandbox and write up the cost analysis instead.
6. **Prometheus + Grafana** — keep OTel and Jaeger; a trace waterfall is a genuinely good demo slide. ~1.5 weeks.
7. **The second paid LLM provider** — make the second implementation local. Two days.
8. **The semantic cache** — cut deliberately and report why. A cache that returns a confidently wrong answer to a subtly different question contradicts a thesis about calibrated refusal. Publish the measured false-hit rate as a negative result.
9. **Blue/green shadow reads** — keep the alias flip, skip the comparison harness.
10. **SCIP** — already descoped. One week, then stop.
11. **Agent tools 6–9** — fold or drop.
12. **Historical chunk indexing** — never index every historical version: 70× corpus multiplication for 99% near-duplicate content. Index HEAD plus the tombstoned final version of each deleted chunk.
13. **`blame -C -C -C`** — measured on this corpus: plain 952 ms, `-M` 948 ms (free), `-M -C` 3,233 ms. `-M -C` alone already recovers a function extracted into an existing file.

---

## 7. The novelty claim, honestly

Function identity across refactors is a fifteen-year-old research area with five named tools and
three published oracles. The spec's framing will not survive a viva unchanged.

- **Historage** (2011) and **FinerGit** (2020) explode each method into its own file so git's rename
  detector tracks it — whole-repo forward lineage indexes. FinerGit ran over 182 projects and 1.77M
  methods. So "no prior tool publishes an all-functions forward index" is false. (It also OOM'd at
  16 GB on the four largest projects — cite that as the scaling risk.)
- **CodeShovel** (ICSE 2021, Distinguished Paper) — 90% complete-history, 99% precision on Java, and
  crucially **ships a Python parser and a 40-method Python oracle** with 327 labelled entries. That
  oracle is your evaluation set; you do not need to hand-label 500 cases.
- **CodeTracker** (FSE 2022) reported 99.9% — self-reported on an oracle later shown faulty. On
  corrected oracles it scores F1 94.30–98.11, sometimes below CodeShovel. Do not repeat 99.9% as fact.
- **HistoryFinder** (2025) — F1 97.36, 400 methods, adds merge-commit handling. Java only.
- **RefactoringMiner 3.1.5** — supports Python at P=0.996/R=0.997, but on 202 commits from three
  small repos dominated by variable-level operations. Quote it with that qualifier.

### The claim that does survive

> CodeShovel, CodeTracker and HistoryFinder solve on-demand backward per-method history for Java at
> 94–99 F1. Historage and FinerGit build forward whole-repo indexes by rewriting the repository.
> **We do not compete on tracking accuracy.** We contribute (a) a typed lineage DAG with N:M
> extract/inline edges and per-edge confidence, built forward over Python without rewriting the
> repository, and (b) — the genuinely new part — **the first measurement of how much
> rationale-bearing provenance a retrieval system recovers when it walks refactor-aware lineage
> instead of git blame.**

Every prior paper's dependent variable is history accuracy. Nobody has measured *provenance-chain
recall*, reported twice — lineage on vs blame-only — as a paired bootstrap. That one ablation table
is the contribution. Title the chapter for the question, not the algorithm.

### Minimum viable version — 22–28 hours, time-box the whole thing at 60

Two hash tiers (`h1` token stream with comments and layout dropped — this absorbs the Black commit
for free; `h3` alpha-renamed AST). A forward pass, same-file only, bucketing on each tier — that
alone covers 63% of the oracle's entries. Then **one** cross-file rule: an exact structural match in
any file touched by the same commit, which covers renames and moves and with them most of the **40%
of oracle methods containing an identity-breaking event**.

That 40% is the most quotable number available: *a path-anchored baseline truncates 40% of Python
function histories.*

**One free figure:** Flask's `full_dispatch_request`. `git log -L` returns 21 commits in 0.29 s, of
which **eight pre-date the function's introduction** (~62% precision, pure line-range drift), and it
omits the 2023 commit that recreated the file. Three citable failure modes, zero extra work.

**Two implementation facts:** do not build on stdlib `ast` alone (5.5% of 2015-era Airflow files are
unparseable under modern Python — use tree-sitter and dead-letter the rest), and `ast.dump` is not
stable across Python minors, so hash the tree-sitter s-expression and stamp the grammar version.

---

## 8. Money

| | Free path | Sane path | Naive path |
|---|---|---|---|
| **Total, 6 months** | **~$25** (₹2,400) | ~$600 (₹57,000 pre-tax) | ~$19,400 (₹18.4 lakh) |

The gap is four decisions, not four purchases: cheap model by default, prompt-cache the stable
prefix, **retrieval-only CI gate**, never run the agent from CI without cassettes. Present the
middle column to your supervisor with the right-hand one beside it — the spec lists "cost per query,
fully accounted" as a deliverable and almost nobody produces it.

- **Kaggle, 30 GPU-hours/week** — the most valuable free resource here. Emit `chunks.parquet`, embed
  on a T4, `COPY` back. The content-addressed `chunk_id` is the join key, so it needs no special-casing.
- **Azure for Students, $100, no credit card** — the zero-forex escape hatch; Indian debit cards are
  frequently declined for AI provider top-ups.
- **Make the repo public** — Actions minutes are unmetered, and hosted runners give 4 vCPU / 16 GB:
  double your cores, four times your usable RAM. Your CI is faster than your laptop.
- **Oracle Always Free** (halved to 2 OCPU / 12 GB in June 2026) is still the only free box big
  enough for Postgres + FastAPI + Jaeger. DigitalOcean's $200 offer ended Aug 2026 — do not plan on it.
- **India:** budget +20–22% on every USD line (18% GST under OIDAR plus card forex markup).
- **Wire hard caps before the first live agent run** — an in-process budget that aborts and records
  `budget_exceeded` as a trajectory outcome, so evaluation counts it as a failure rather than
  silently retrying.

**Licences to check before anything reaches the thesis.** `jina-embeddings-v3`, the Jina code
models and `Bespoke-MiniCheck-7B` are CC-BY-NC. `MiniCheck-Flan-T5-Large` is MIT and
`HHEM-2.1-open` is Apache-2.0. ParadeDB is AGPL-3.0. More important: **GitHub discussion text is not
Apache-2.0** — it is the authors' copyright, and Airflow's licence does not cover it. Ship IDs and a
fetch script, never a comment dump. See [DATA-PROTECTION.md](DATA-PROTECTION.md).

---

## Related

- [tracker/](tracker/README.md) — per-subsystem work tracking, and
  [000_REVIEW_REMEDIATION](tracker/000_REVIEW_REMEDIATION.md), the 20 findings from the 2026-10-06
  review of this document. **This file stays the only place weeks are assigned**; the trackers
  mirror the code, not the calendar
- [ADR-0001](ADR-0001-typed-edges.md) — typed edge tables over one polymorphic table
- [ADR-0002](ADR-0002-golden-set-construction.md) — the anchor symbol comes from code, never the rationale
- [RUNBOOK.md](RUNBOOK.md) — starting everything, degradation behaviour, reading a trace
- [DATA-PROTECTION.md](DATA-PROTECTION.md) — what personal data is processed, and what is excluded
- [../provenance/eval/README.md](../provenance/eval/README.md) — why the CI gate splits in two

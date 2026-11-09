# ADR-0005: Lineage against blame — the provenance-chain experiment, fixed before it runs

- **Status:** Accepted (2026-11-08) — committed with [`PREREGISTRATION.md`](PREREGISTRATION.md),
  before any blame, lineage or comparison number exists
- **Date:** 2026-11-08
- **Implements:** review finding 3 ([tracker 000](tracker/000_REVIEW_REMEDIATION.md)) — a
  volume-matched control, a dependent variable in code, and falsifiers — and
  [tracker 004](tracker/004_FUNCTION_IDENTITY.md) task 2, the oracle check
- **Refines:** [ROADMAP](ROADMAP.md) §5 checkpoint C and §7
- **Applies to:** `docs/PREREGISTRATION.md`, `provenance/eval/metrics.py`, the blame arm (slice
  C2), the identity layer (C3), `pace eval provenance` (C4)

## Context

ROADMAP §7 narrows the novelty claim to one measurement: *how much rationale-bearing provenance a
retrieval system recovers when it walks refactor-aware lineage instead of git blame*, reported as
a paired comparison. Review finding 3 found three gaps in it: the lineage arm reaches four to six
times more commits than blame, so a gain is confounded with volume; the dependent variable exists
nowhere in code; and nothing states, before the result, what outcome would count as failure.

**What checkpoint C starts from (measured 2026-11-08):**

- **The questions.** Checkpoint B's 4,206 retained dev records (`auto_full.jsonl`). Each names a
  function by its path and qualified name at the pinned commit `1e2ad803f7`, and its gold is the
  commit (and linked PR) of one change to that function. The gold's validity is 95.5%, 95%
  interval 84.9% to 98.8% ([ADR-0004](ADR-0004-gold-validity-audit.md)).
- **Mechanical commits.** Checkpoint A marks them at ingest (`commits.is_mechanical`): 2,223 of
  38,888.
- **Blame.** Measured on this corpus (tracker 003): 952 ms plain, 948 ms with `-M`, 3,233 ms with
  `-M -C`. ROADMAP trap 3: 29% of blamed lines land on a mechanical commit.
- **Lineage.** Migration `0002_lineage` defines `symbol_lineage`, `symbol_versions`,
  `lineage_edges` and `blame_ranges`. All four are empty.
- **The oracle check.** CodeShovel (Grund et al., ICSE 2021), commit `0d29f49` (2024-01-31), MIT
  licence, ships `src/test/resources/oracles/python/`: 40 functions, four from each of ten
  projects (asciinema, django, flask, httpie, keras, models, pandas, rebound, scikit-learn,
  zulip), with 327 labelled commits — one `Yintroduced` per function, 207 `Ybodychange`, 50
  `Ymultichange`, 13 `Yfilerename`, 7 `Yparameterchange`, 4 `Yparametermetachange`, 4
  `Ymovefromfile`, 1 `Yrename`, and one entry misspelt `Ybodychagne`. 16 of the 40 functions
  hold a rename or a move (file renames 12, moves from another file 6, function renames 7;
  overlapping). Tracker 004's fallback, 500 hand labels, is not needed.

**The tilt.** The generator keeps a question only if its qualified name exists at HEAD in the
same rename-aware file ([tasks/eval-harness/003](tasks/eval-harness/003_golden_set_generator.md)
rule 1). So every gold change happened under the function's current name and file. On this set,
lineage can still beat blame by reaching changes whose lines were later overwritten — blame sees
only the last commit to touch each line — but its identity tracking can never be what finds a
gold, and its commits from before a rename or move can only dilute the equal-size control below.
The design states this rather than hides it, and measures identity tracking where it matters:
on the oracle.

## Decision

### 1. The questions

The 4,206 retained dev records. Each starts from its function at the pinned commit, located by
its symbol anchor through the tree-sitter symbol parser; rule 1 guarantees it exists. Every arm
starts from the same function and its same line span. The test split stays sealed.

### 2. The arms

Each arm turns a question's function into a set of commits. Mechanical commits are excluded from
every arm.

- **A — blame.** The commits `git blame` (§6) names for the function's current lines.
- **C — lineage, full.** Every commit in the function's stored chain (§7) where it was born,
  changed, renamed or moved.
- **B — lineage, equal size.** The exact probability that a uniformly random subset of C's
  commits, as large as A's, contains the gold:
  `B = min(1, |A| / |C|) · 1[gold ∈ C]`, and 0 when C is empty. It is computed, not sampled,
  so the result holds no randomness.
- **L — `git log -L`** on the function's line span at HEAD: reported beside the arms, claimed
  nothing about. It answers "why not `git log -L`?".

### 3. The dependent variable

*Provenance-chain recall*: the mean over questions of `1[gold ∈ arm's commits]` — for B, the
probability above. A record's commit unit and PR unit belong to one change, so reaching its commit
reaches both, and per-question recall is 0 or 1 for A, C and L. `metrics.py` implements it, with
the equal-size rule fixed in code: no argument changes how B is sized.

### 4. Statistics

As in slice B5: bootstrap intervals stratified by query class; paired differences C − A (the
primary claim), B − A (the control) and C − B (the share due to volume), with L − A reported; D
and the MDE beside every difference between two 0-or-1 arms; and every difference divided by the
gold's validity at its point estimate and both ends of its interval (ADR-0004 §8).

### 5. The claims, fixed here and in `PREREGISTRATION.md`

The thresholds are constants in code, and a unit test checks they equal the numbers written in
`PREREGISTRATION.md`. The comparison's output states, for each claim, whether it held.

1. **Coverage, the primary claim.** C beats A by at least 0.05, with the paired 95% interval's
   lower end above 0. Otherwise the claim failed.
2. **Selection, the control.** Predicted: B ≤ A, because of the tilt. B − A with an interval
   above 0 means lineage picks better commits even on a set tilted against it; an interval
   containing 0 means its gain is volume; an interval below 0 means blame is more precise per
   commit, and lineage's gain is coverage alone.
3. **Identity, on the oracle.** Lineage's commit-set F1 on the 40 functions beats `git log -L`'s
   by at least 0.05, paired over the functions, with the bootstrap interval's lower end above 0.
   The 16 functions holding a rename or move are reported separately.
4. **Finding 3's other two claims**, recorded now and tested where they belong: grep's retained
   recall@10 stays below 0.35 on the test split at its final opening (ADR-0003 §6's gate); and
   refusal buys accuracy, in checkpoint D, against two trivial abstainers.

### 6. Blame

`git blame --porcelain -w -M -L <start>,<end> --ignore-revs-file <list> <pinned> -- <path>` in the
corpus clone. `-w` ignores whitespace-only changes and `-M` follows lines moved within a file. The
list holds the mechanical commits, read from the database at run time. There is no `-C`: finding
copies across files costs 3.4 times as much, and crossing files is exactly what lineage's
cross-file rule adds. Ranges go into `blame_ranges`, whose exclusion constraint forbids two
owners for one line. The run skips functions already stored, so it resumes. `git log -L` shares
the module, since three slices use it.

### 7. Lineage, the minimum viable version

One engine walks a file's history commit by commit, parses each version with tree-sitter, and
decides identity in this order:

1. the same qualified name in the same rename-aware file is the same function;
2. among the leftovers in that file, an equal `h1` (tokens without comments or layout) is a rename
   with an unchanged body, then an equal `h3` (structure with every name replaced by a
   placeholder) is a rename with local renames;
3. **the one cross-file rule:** a function still unmatched is a move if an identical `h3`
   disappeared from another file touched by the same commit;
4. anything else is a birth; a function that disappears is a death.

A hash matching more than one candidate is not linked, and is counted. Hashes are computed over
the tree-sitter syntax tree and stamped with the grammar version (`ast_norm_version`); a file that
does not parse is dead-lettered. **Detecting a change is separate from matching identity:** a
version row is written whenever the function's text changes, comments included and whitespace
not, because CodeShovel's oracle counts comment and reformatting edits as body changes. Mechanical
commits keep their versions, flagged, and the arms exclude them. The same engine runs over
Airflow's 303 in-scope files, writing the lineage tables, and over the oracle functions' file
histories, writing nothing. Extract and inline edges are outside this version; `lineage_edges`
can hold them later.

### 8. How lineage is judged

- **On the oracle:** commit-level precision, recall and F1 per function, for lineage and for
  `git log -L`, compared paired over the 40, with the 16 rename-or-move functions apart. The ten
  projects are cloned outside the repository and their oracle files are read as data.
- **On Airflow:** agreement with `git log -L` on 100 stratified functions, at least 0.9
  commit-set F1 (tracker 003), reported apart for functions whose span `git log -L` follows
  across the 2025 directory move and those it does not.

### 9. The run, gated mechanically

`pace eval provenance` writes nothing unless `docs/PREREGISTRATION.md` is committed and unmodified
— the results record its commit — every question's function resolves at the pinned commit, every
function has stored blame and a stored lineage, and the gold's validity is current. Two runs write
the same bytes.

## Consequences

- C's comparison measures what B's questions can show: coverage of a function's history. The
  identity layer's own value is measured on the oracle, where 16 of 40 functions need it.
- ROADMAP §5's gate for C becomes mechanical: the comparison cannot run before the
  pre-registration is committed, and the volume-matched arm is in the same table as the
  unbudgeted one.
- The thesis reports the decomposition C − A = (C − B) + (B − A) whatever its sign: more commits
  against better commits.

### Known limits, stated rather than hidden

- **The tilt** (Context). An equal-size win for lineage is unlikely on this set by construction.
- **The gold** is 95.5% valid, not 100% (ADR-0004); every difference is also quoted divided by it.
- **`git log -L`** may stop at the 2025 directory move; slice C2 measures whether it does.
- **Blame without `-C`** misses lines copied from other files, for the cost above.
- **The oracle** is 40 functions from ten other projects; how well its accuracy transfers to
  Airflow is argued, and the 100-function agreement check is the only Airflow-side measure.
- **Extract and inline** are not detected; a function split in two continues in one.

## Alternatives considered

- **Run CodeShovel itself on Airflow** to find renames and moves independently, and ask about
  changes from before them. Rejected for checkpoint C: about a week of work, and CodeShovel's own
  error rate becomes noise in the gold. It is the natural next experiment.
- **Hand-check a sample of the renames and moves our lineage finds,** and ask about those.
  Rejected: it covers only breaks our own code found, which favours lineage.
- **A fourth, placebo arm** with the same edge count and degree distribution, targets rewired to
  random commits of the same era (finding 3's note). Rejected as beyond a solo project's budget;
  the review called three arms the right call.
- **The equal-size arm takes lineage's newest commits.** Rejected: blame's commits are mostly
  recent, so the arm would repeat A.
- **Both arms rank their commits by relevance to the question and keep the top 10.** Rejected for
  the control: the result would also measure the ranker.
- **Trace each function backwards on demand,** as CodeShovel does. Rejected: it drops the stored
  forward lineage, half of the contribution, and repeats 4,206 history walks per run.
- **Track functions by name alone.** Rejected: it sees no rename or move.
- **`git blame -C -C -C`.** Rejected for cost, above.
- **500 hand-labelled functions** as tracker 004's fallback. Not needed: the oracle exists.

## Errata

- **2026-11-09 — what `--ignore-revs-file` does with a line the ignored commit added (slice
  C2).** The first addendum below says git "cannot re-attribute a line the ignored commit
  added". Slice C2's fixture showed otherwise: git attributes such a line to the commit of a line
  near it when it can (git-blame(1): "blamed on the previous commit that changed that line or
  nearby lines"), and leaves on the ignored commit only a line it cannot place
  ([tasks/symbols-blame/001](tasks/symbols-blame/001_blame_arms.md)). The rule that addendum fixes
  is unchanged: a line git leaves on a mechanical commit is stored with no filtered owner. On the
  benchmark's functions that is 149 of 40,071 lines.
- **2026-11-09 — what lineage counts as a change, and how it compares names (slice C3a).
  Committed before any lineage number.** §7 says a version is written when a function's text
  changes, "comments included and whitespace not, because CodeShovel's oracle counts comment and
  reformatting edits as body changes". That reason is wrong. CodeShovel's Python body is the
  ANTLR text of the function's suite, and its pinned grammar (antlr/grammars-v4 `8803cbe8`,
  `python/PythonLexer.g4`) sends comments and whitespace to the hidden channel, so its body change
  is a change in the token stream. Docstrings are string tokens and do count. CodeShovel also
  labels a commit that only renames the function's file. So, replacing §7's sentence:
  - **A version is written** at birth, when the function's token stream changes (the ordinary h1:
    code and docstrings count, comments and layout do not), when its qualified name changes, and
    when its path changes by a file rename or a move.
  - **Identity hashes are name-blind.** h1 and h3 include the function's own name (h3 renames
    locals, not the function itself), so a pure rename would change both. §7's "an equal h1" and
    "an equal h3" are therefore computed with the definition's own name replaced by a placeholder.
  - **Stages.** `symbol_versions.match_stage` records how each version kept its identity: `birth`,
    `name`, `h1`, `h3` or `move` (migration 0011).

  §7's matching order, its one cross-file rule and its ambiguity rule are unchanged, and so are
  §1–§6, §8 and §9 ([tasks/function-identity/002](tasks/function-identity/002_lineage_engine.md)).

## Addenda

- **2026-11-09 — blame and `git log -L` follow first parents, and how slice C2 runs and stores
  them. Committed before any blame number.** Designing slice C2
  ([tasks/symbols-blame/001](tasks/symbols-blame/001_blame_arms.md)) found that §6's command can
  name commits checkpoint A never stored. The changes:
  - **`--first-parent` on both commands.** The corpus has 40,995 commits; checkpoint A's graph
    holds the 38,888 on the first-parent line, and every gold commit is one of them. Plain blame
    can name any of the other 2,107: `blame_ranges` rejects them, and no question can reach them.
    With `--first-parent`, a change made on a side branch is credited to the merge commit that
    brought it in, as the gold is. Arm A runs `git blame --porcelain -w -M --first-parent -L
    <s>,<e> --ignore-revs-file <list> <pinned> -- <path>`. Arm L runs `git log --first-parent -L
    <s>,<e>:<path> --format=%H -s <pinned>`, and drops mechanical commits afterwards.
  - **One blame call per file,** with one `-L` per function. Each line gets the owner a call per
    function would give it: in the three files with the most functions, none of 4,573 lines
    differ.
  - **Both owners are stored.** Each file is blamed once without the list and once with it.
    `orig_commit_sha` holds the plain owner. `effective_commit_sha` holds the filtered one, or
    NULL when git can only name a mechanical commit: `--ignore-revs-file` cannot re-attribute a
    line the ignored commit added. Arm A reads the filtered owner. The plain one gives a
    labelled row that is not pre-registered (tracker 003 row 2), and re-measures trap 3.
  - **A function's lines** run from its first decorator to its last line. A getter and a setter
    under one name give two ranges.
  - **What was measured before this addendum:** timings and owner counts on six sampled
    functions, the one-call-per-file check, and the 2025 move on one function, where both tools
    followed `243fe86d4b`. Nothing was scored against gold.

  §1's questions, §3's measure, §4's statistics and §5's claims are unchanged.
- **2026-11-09 — the 2025 move, measured (slice C2).** Known limits asked whether `git log -L`
  stops at `243fe86d4b`. Of the 1,061 question functions that existed before it, `git log -L`
  lists commits from before the move for 1,049, and blame has an owner at the old path for 1,035
  ([results/blame_arms.json](../results/blame_arms.json)): neither tool stops there. A function
  can have no owner from before the move simply because all of its lines were rewritten since.
  PREREGISTRATION.md's agreement check reports the functions `git log -L` follows across the move
  apart from the rest, as it says.
- **2026-11-09 — the agreement check, measured, and a limit it shows (slice C3a). Recorded
  before the oracle is run.** On PREREGISTRATION.md's 100 Airflow functions, lineage's commit
  sets agree with `git log -L`'s at a mean F1 of 0.771 (95% bootstrap interval 0.721 to 0.817),
  short of `AGREEMENT_MIN_F1` (0.90). The check is missed, and as the pre-registration says, that
  qualifies claims 1 to 3 ([results/lineage.json](../results/lineage.json)). Its breakdown of the
  differences (`agreement.breakdown_not_preregistered`) was added after the miss and changes no
  earlier number. 414 commits are in both sets. The readings below, made case by case, are not
  pre-registered either.
  - **274 commits only `git log -L` lists.** 57 come after the function's birth, at commits where
    lineage records no change to it. In most of these, nothing in the function changed:
    `git log -L` saw a change on lines outside it. The rest edit only its comments or layout. The
    other 217 come before lineage's birth for their function.
  - **198 commits only lineage lists.** 66 change only the function's path, nearly all by a file
    rename, which `git log -L` does not list; most are at the 2025 move (`243fe86d4b`), which is
    not a mechanical commit. 123 change the function's tokens or name where `git log -L`'s line
    range missed the changed lines. In over a third, the function later moved to another file,
    which `git log -L` does not follow, and a few come before the 2025 move, where it stops. In
    the rest, its range had drifted off those lines. 9 are births.
  - **The 25 functions with commits before their birth.** In 14, another function died in the
    same file at the birth commit. About half of those 14 are a rename made together with a body
    edit: the dead function's code reappears, edited, under the new name. Neither name-blind hash
    links that, so lineage splits one function into two identities. That is a limit of §7's
    matching. The other functions among the 25 were extracted from another function, which
    lineage starts as a new identity (extract edges come later), or are new or rewritten code.
    For those, `git log -L` carries on into the history of the lines they came from or replaced.

  The engine is frozen as built (`lineage-mvp-1`). Slice C3b runs it on CodeShovel's oracle with
  this limit stated in advance. Similarity matching, which could link a rename made with an edit,
  is future work.
- **2026-11-09 — the oracle run, fixed before it (slice C3b).** Designing slice C3b
  ([tasks/function-identity/003](tasks/function-identity/003_oracle_run.md)) found that §7's
  "over the oracle functions' file histories" leaves the cross-file rule nothing to link, and
  that §8 leaves the units and the side-branch labels open. The rules, committed before the
  oracle is run:
  - **The files walked.** For each function: its own rename-aware file, then every Python file
    touched by a commit where a piece of its tracked identity begins, until nothing is added. The
    cross-file rule links a birth and a death at one commit, both in files that commit touched,
    so this gives the identity a walk over the whole project would. File histories are rebuilt in
    memory with checkpoint A's own walk, and nothing is written to the database.
  - **The units.** Lineage is scored through the identity whose span holds the oracle function's
    `def` line at its start commit: the function itself, the enclosing function for the 3 nested
    ones, and the property for the 2 property parts. `git log -L` gets the function's own lines,
    decorators included, as it would be used. Any bias from the coarser unit falls on lineage.
  - **The labels.** A label off the start commit's first-parent line counts at the merge that
    brought it in, where a first-parent walk first sees the change: 61 labels in 22 functions.
    Both arms walk first parents.
  - **No mechanical filter:** these projects have no mechanical list, and CodeShovel labels a
    reformat that changes tokens.
  - **The 16** rename-or-move functions are those with a `Yrename`, `Yfilerename` or
    `Ymovefromfile` label, counting those inside a `Ymultichange`.
  - **One run,** on the frozen engine, repeated once unchanged to show the same bytes. A defect
    found afterwards in the run's own code is fixed and the run repeated, and both runs are
    reported.
  - **The transfer.** On Airflow the engine walks only the 303 in-scope files, so a function moved
    in from any other file is a birth there. The oracle measures the engine with every file its
    chains need, and claim 3's result is quoted with that difference.

## References

- Grund, Chowdhury, Bradley, Hall, Holmes. *CodeShovel: Constructing Method-Level Source Code
  Histories.* ICSE 2021. Repository `ataraxie/codeshovel`, commit `0d29f49`, MIT licence.
- Kim, Zimmermann, Pan, Whitehead. *Automatic Identification of Bug-Introducing Changes* (AG-SZZ).
  ASE 2006 — the source of trap 3's blame correction.
- da Costa et al. *A Framework for Evaluating the Results of the SZZ Approach.* TSE 2017 — the model
  for evaluating a blame correction.
- ROADMAP §7 for FinerGit, CodeTracker and HistoryFinder; review finding 3 and its task,
  [tasks/review-remediation/003](tasks/review-remediation/003_control_arms_and_preregistration.md).

# Task 001: Make §5 an executable schedule

**Tracker:** [000_REVIEW_REMEDIATION](../../tracker/000_REVIEW_REMEDIATION.md) · finding 1
**Priority:** P0 · **Severity:** critical · **Status:** ⬜ Not started

## User Story

- **Summary:** Week 3 — the reordering §2 calls "the single most important" in the plan — is
  topologically downstream of weeks 4-7 on four independent paths. The week table cannot be
  executed in the order it is written.

### Use Case

- **As the** author following this plan week by week
- **I want** week 3's deliverable to depend only on weeks 1-2's output
- **so that** the benchmark is validated before five weeks of work are built on it, which is the
  entire argument for moving it forward.

### Background (traced)

Every input the week-3 deliverable needs is produced later:

| Week-3 input | Produced by |
|---|---|
| Anchor via `symbol_versions` (ADR-0002 decision step 1) | Symbol layer — weeks 6-7 |
| PR→issue `closes` edge | GitHub backfill — weeks 4-5 |
| `baseline_grep_r10`, `gold_symbol_df` over the in-scope pool | Git walk — weeks 4-5 |
| `anchors.py` resolution via `chunk_evidence`; `leakage_probe.py` pool from `SELECT … FROM chunks` | Chunking — weeks 6-7 |

ADR-0002 contradicts itself by four weeks: its decision step 1 mandates `symbol_versions`, its own
header (line 5) says *"Applies to: the golden-set generator (week 3)"*.

This is already observable. A local `leakage_probe.json` run drops all 3 CI records with
`anchors_resolve_to_nothing`, every baseline recall@10 is 0.0, `gold_symbol_df` is null.

**The knock-on is the dangerous part.** The week-2 gate reads *"grep recall@10 < 0.35 on the
retained set"* — and **0.0 < 0.35**, so it passes *vacuously* on an empty retained set.
`leakage_probe.py` guards this correctly (INCONCLUSIVE, exit 2); the roadmap table does not, and the
table is what gets read in week 2.

Two compounding errors sit in the same table. Weeks 6-8 budget chunking, symbols, blame and
function identity as three weeks — exactly the estimating error §2:54-55 diagnoses in the spec
("four separately hard problems budgeted as one, at about 2.6-3.2× under") — while §7:270
time-boxes function identity *alone* at 22-28 h capped at 60. And §6's thirteen cut items were all
excluded before the week table was written, so "delete from the top as time runs out" recovers no
scheduled time at all.

### Acceptance Criteria

- **Scenario:** Week 3 is executable from week 2's output
  - **Given:** the re-cut schedule
  - **When:** week 3 begins
  - **Then:** its deliverable is a *minimal vertical slice* its own prerequisites support — a
    path-anchored, AST-only anchor extraction over a small git-walk subset — with the
    `symbol_versions` upgrade explicitly deferred to week 7.

- **Scenario:** ADR-0002 stops contradicting itself
  - **Given:** the deferral above
  - **When:** ADR-0002's header is read
  - **Then:** it states which part applies in week 3 and which in week 7.

- **Scenario:** The week-2 gate cannot pass on nothing
  - **Given:** the gate wording in the §5 table
  - **When:** the retained set is empty
  - **Then:** the table itself requires `n_retained >= N`, matching what `leakage_probe.py` already
    enforces — the guard belongs in both places, because the table is what a human reads.

- **Scenario:** Weeks 6-8 carry the multiplier the document itself derived
  - **Given:** §2's own 2.6-3.2× under-costing finding
  - **When:** weeks 6-8 are re-budgeted
  - **Then:** the estimate applies that multiplier, or the scope of those weeks is cut until it fits.

- **Scenario:** §6 can actually rescue the schedule
  - **Given:** the cut list
  - **When:** a week slips
  - **Then:** the top of the list names something **currently scheduled**. Candidates: the agent's
    tools 3-5, the SCIP ablation row, the idiom-clustering reframe of Cross-cutting (§4 trap 4).

## Files to Modify

- `docs/ROADMAP.md` §5 (week table, weeks 3-8 and the week-2 gate wording), §6 (cut list)
- `docs/ADR-0002-golden-set-construction.md` header and decision step 1

## Notes

- Do this in **one editing pass** with findings 5 (writing schedule) and 10 (rate-limit budget in
  weeks 4-5). All three change the same table; three separate passes will produce three
  inconsistent versions of it.
- The vacuous-gate problem is the most quietly dangerous item in the whole review, because it is a
  *passing* gate. The project's own standard — "a check that cannot fail is not a check" — already
  names the failure mode; this is the same bug one layer up, in prose instead of code.

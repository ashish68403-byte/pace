# Tracker: Roadmap Review Remediation

**Task(s):** [docs/tasks/review-remediation/](../tasks/review-remediation/)
**Priority:** P0
**Status:** 🚧 In progress (0 of 20 closed)
**Date:** 2026-10-06

---

## Summary

A seven-lens review of [ROADMAP.md](../ROADMAP.md) on 2026-10-06 produced 32 findings; one was
dropped as already addressed by the document, 12 were downgraded as overstated, and 31 survived an
adversarial audit that independently re-ran the chunker, re-derived the corpus scope and checked
the cited literature. This tracker carries the 20 that were kept after merging duplicates across
lenses.

The findings are not about the roadmap's rigour, which is high and verified — three of Trap 2's
four numbers reproduce to the digit against the clone at `1e2ad803f7`. They cluster in one place:
**the document applies reviewer-grade scepticism to every claim except its own experiment design.**

## Problem Statement

Four of these cannot be fixed later, and that is the only reason this tracker is P0:

1. **A held-out split cannot be designated retrospectively.** Once CI has shown you the full set
   for six weeks, no half of it is held out any more.
2. **A control arm is an experiment, not a paragraph.** The lineage-vs-blame comparison needs a
   volume-matched control *run*, and week 8 is where that becomes expensive.
3. **A pre-registered falsifier is only evidence if it predates the result.** Its value is its
   position in git history.
4. **Per-query telemetry cannot be reconstructed.** If week 11's agent runs do not log fused
   scores for refused queries, finding 15 is unanswerable forever.

Everything else here is repairable at any point. These four are not.

## Task Breakdown

Severity is the audited severity. "Before week 3" means it gates the next ringed gate in §5.

### Before week 3

| # | Finding | Sev | Status | File |
|---|---|---|---|---|
| 1 | [§5 is not an executable schedule; §6 cannot rescue it](../tasks/review-remediation/001_executable_schedule.md) | critical | ⬜ | `docs/ROADMAP.md` §5, §6; `docs/ADR-0002` header |
| 2 | [No dev/test split; optimistic bias ≈ the claimed lift](../tasks/review-remediation/002_dev_test_split.md) | major | ⬜ | `provenance/eval/schema.py`, `runner.py`, `retrieve/fusion.py` |
| 3 | [Week-8 comparison has no control, no dependent variable, no falsifier](../tasks/review-remediation/003_control_arms_and_preregistration.md) | critical | ⬜ | `provenance/eval/metrics.py`, `docs/PREREGISTRATION.md` (new) |
| 4 | [Corpus is 480 files / 112,259 lines; `scope.txt` never committed](../tasks/review-remediation/004_scope_freeze.md) | critical | ⬜ | `provenance/ingest/scope.py`, `.github/workflows/ci.yml` |
| 5 | No writing scheduled before week 15; the graded artifact is on no week and no cut list | critical | ⬜ | `docs/ROADMAP.md` §5, §6 |
| 6 | The load-bearing premise ("a merged PR that closed an issue IS the rationale") has no measurement | critical | ⬜ | gold-validity audit, new ADR |
| 7 | CI gate cannot detect 2 points; its denominator moves with the chunker | major | ⬜ | `provenance/eval/runner.py`, `metrics.py`, `anchors.py` |
| 8 | Leakage gate has three ways to pass green; the thesis stratum is dropped before aggregation | major | ⬜ | `scripts/leakage_probe.py`, `runner.py` |
| 9 | The week-8 oracle may not exist; the most-quoted number is n=40 with no interval | critical | ⬜ | `docs/ROADMAP.md` §7, new ADR |
| 10 | Weeks 4-5 spend ~29 h of rate-limited fetching for ~25 min of work; no request budget | major | ⬜ | `scripts/fetch_corpus.py`, `docs/ROADMAP.md` §5 |
| 11 | Ethics self-assessment has no date; weeks 4-5 are first processing of human text | major | ⬜ | `docs/DATA-PROTECTION.md` §7, `docs/ROADMAP.md` §5 |
| 12 | The laptop is the only copy of derived state; nothing in 16 weeks produces a dump | major | ⬜ | `Makefile`, `docs/RUNBOOK.md` |

### Before the write-up

| # | Finding | Sev | Status | File |
|---|---|---|---|---|
| 13 | §7 surveys the field the project is not in (SZZ, traceability, commit→issue linking) | major | ⬜ | `docs/ROADMAP.md` §7 |
| 14 | Week-13 refusal numbers are bare point estimates on n≈40; a committed row says 0.00 on an unscoreable class | major | ⬜ | `provenance/eval/runner.py`, `metrics.py` |
| 15 | Refusal scored only against its own label; no evidence it beats a score threshold | major | ⬜ | `provenance/eval/runner.py` (`PER_QUERY_COLUMNS`) |
| 16 | 6% of HEAD chunks exceed the embedder window; splitting drops comments | major | ⬜ | `provenance/parse/chunker.py` |
| 17 | "Warm-resident reranker" is a hard constraint on no week and no cut list | major | ⬜ | `provenance/retrieve/`, `api/main.py`, `docs/ROADMAP.md` §6 |
| 18 | Two of Trap 2's numbers have no reproducible method; 0.986/n=358 has no committed derivation | major | ⬜ | `docs/ROADMAP.md` §4, `artifacts/corpus_facts.json` (new) |
| 19 | The Status block has three checkable errors | minor | ⬜ | `docs/ROADMAP.md` lines 18-25, `tests/integration/test_schema.py` |
| 20 | "Every number reproduces from one command" has no defined bundle | minor | ⬜ | `docs/ROADMAP.md` §5 week 15-16 |

## Dependencies

- Findings 2, 3, 7 and 15 are **ordered before** the work they constrain: 2 and 7 before the week-3
  generator fixes its target `n`; 3 before week 8 runs; 15's logging before the first week-11 agent run.
- Finding 4 blocks any recall number, since the scope file list is the denominator.
- Finding 1 blocks the week-3 row itself, and findings 5 and 10 change the week table with it —
  do them in one editing pass rather than three.

## Verification Steps

Per-task criteria live in the task files. Tracker-level:

1. `uv run pace scope --check` passes against a committed `scope.txt` (finding 4).
2. `uv run pytest tests/ -q` stays green; new invariants from findings 7, 14 and 16 have tests.
3. `make ci-local` (or the `retrieval-gate` replay) passes end to end.
4. Every ROADMAP number changed by this tracker has a command recorded beside it (finding 18).

## Files Affected

Populated as tasks close.

## Notes

- **2026-10-06.** Review run as a seven-lens workflow (feasibility, evaluation statistics,
  novelty/related work, architecture, roadmap-vs-code, completeness, viva examiner), each lens
  followed by an adversarial audit instructed to drop anything the document already handles.
  Verdict mix: 19 upheld, 12 overstated-but-kept, 1 dropped. The dropped one claimed week 2
  overstated what was verified; the audit correctly pointed at ROADMAP:27-29, which already says
  "every evaluation number is structurally zero", and at the cell reading "**skeleton** DONE"
  rather than "DONE".
- **2026-10-06.** The audit independently reproduced several claims rather than reasoning about
  them: it ran the real chunker (5,601 chunks; 367 of 4,881 `(path, qualified_name)` keys resolve
  to more than one chunk), re-derived the scope both ways (480/112,259 as committed, exactly
  303/81,619 with `**/api_fastapi/**` added), and created a scratch database from `template0` to
  settle the table count (26 PACE tables + 1 view; the live 28 comes from `template1`'s inherited
  PostGIS `spatial_ref_sys`).
- **2026-10-06.** Findings the review explicitly declined to raise are recorded in its "Worth
  knowing" section — including that a fourth placebo arm for week 8 would be the strongest control
  and is correctly out of budget for a solo project. Three arms is the right call.

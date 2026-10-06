# Tracker and tasks

Two directories, one job each.

| | |
|---|---|
| `docs/tracker/NNN_NAME.md` | One file per **workstream**. Status, dependencies, a task breakdown table, verification steps, and a dated Notes log. This is what you read to answer "where is this up to". |
| `docs/tasks/<slug>/NNN_name.md` | One file per **task**. User story, Given/When/Then acceptance criteria, files to modify, and the change itself. This is what you read to do the work. |

A tracker links down to its task folder; every task links back up. Numbering is stable and never
reused — a dropped task keeps its number and gets `Status: ❌ Dropped` with the reason, because the
*reason* a thing was not done is worth more in a viva than the fact that it wasn't.

## What this is NOT

**It is not the schedule.** [ROADMAP.md](../ROADMAP.md) §5 is the only place weeks are assigned.
Trackers mirror the *code*, not the calendar. Duplicating the week table here would create exactly
the drift the roadmap's own commit message warns about: "a plan that drifts from the code it plans
is worse than no plan". A tracker says what a subsystem needs and what state it is in; the roadmap
says when.

## Status vocabulary

| Symbol | Meaning |
|---|---|
| ⬜ Not started | No code, no decision |
| 🚧 In progress | Started, not meeting its acceptance criteria yet |
| ✅ Done | Acceptance criteria met **and verified by a command recorded in Verification Steps** |
| ⛔ Blocked | Waiting on another task — name it |
| ❌ Dropped | Deliberately not doing it. Record why; this is evidence, not failure |

"Done" means a command was run and its output seen. The project's own thesis is that an unverified
number is not a number; the same rule applies to a checkbox. A gate that was never executed is
⬜, not ✅ — see [000_REVIEW_REMEDIATION.md](000_REVIEW_REMEDIATION.md) finding 19 for what happens
when that distinction slips.

## Priority

`P0` blocks a ringed gate in ROADMAP §5 · `P1` blocks a week · `P2` wanted before the write-up ·
`P3` nice to have.

## Index

| Tracker | Scope |
|---|---|
| [000_REVIEW_REMEDIATION](000_REVIEW_REMEDIATION.md) | The 2026-10-06 roadmap review: 20 findings, prioritised |
| [001_INGEST](001_INGEST.md) | Git walk, GitHub backfill, scope freezing, dead letters |
| [002_CHUNKING](002_CHUNKING.md) | tree-sitter AST chunking, token budget, chunk identity |
| [003_SYMBOLS_BLAME](003_SYMBOLS_BLAME.md) | Symbol extraction, blame, mechanical-commit filtering |
| [004_FUNCTION_IDENTITY](004_FUNCTION_IDENTITY.md) | The lineage DAG and the novelty claim |
| [005_RETRIEVAL](005_RETRIEVAL.md) | BM25, dense, RRF, reranking, the ablation table |
| [006_EVAL_HARNESS](006_EVAL_HARNESS.md) | Golden set, leakage gate, metrics, CI |
| [007_AGENT_REFUSAL](007_AGENT_REFUSAL.md) | Agent loop, grounding verification, refusal |

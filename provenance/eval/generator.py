"""Slice B3's golden set: every candidate question, measured for leakage, and its census.

`pace golden generate` turns the in-scope history's symbol events (`eval.candidates`)
into `GoldenRecord`s, measures each one's leakage against the commit-message pool with
the verdict's own code (`eval.leakage`), and writes two files
(docs/tasks/eval-harness/003_golden_set_generator.md):

* `provenance/eval/golden/auto_full.jsonl` -- every candidate, sorted by qid, with its
  leakage facts; `passes_leakage_filter` says which are retained (ADR-0003 §5);
* `results/golden_census.json` -- counts and identifiers only: what each rule removed,
  what the leakage filter removed, and the split.

Nothing is sampled (ADR-0003 §5), so the retention rate is a census figure. The same
graph, pool and corpus always give the same bytes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Final

from provenance.eval.candidates import ADDED, CHANGED, Event
from provenance.eval.leakage import LeakageFacts
from provenance.eval.schema import (
    GoldenRecord,
    GoldEvidence,
    LeakageProbe,
    QueryClass,
    Split,
    SymbolAnchor,
    passes_leakage_filter,
)

GENERATION_STRATEGY: Final = "ast_diff_v0"

#: Each template's query class (ADR-0003 §3).
QUERY_CLASS: Final[Mapping[str, QueryClass]] = {
    ADDED: QueryClass.PROVENANCE,
    CHANGED: QueryClass.ARCHAEOLOGY,
}
TEMPLATES: Final[Mapping[str, str]] = {
    ADDED: "Why was `X` added to `path`?",
    CHANGED: "Why did `X` change in <Month YYYY>?",
}

#: Why ADR-0003 §5 drops a generated record, in the order the filter checks.
RANK_1: Final = "grep_answers_at_rank_1"
RARE_TOKEN: Final = "rare_token_shared_with_gold"
UNMEASURED: Final = "unmeasured"
DROP_REASONS: Final = (RANK_1, RARE_TOKEN, UNMEASURED)

#: English month names, fixed: `strftime("%B")` follows the machine's locale.
_MONTHS: Final = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def month_label(month: str) -> str:
    """`2023-03` -> `March 2023`."""
    year, number = month.split("-")
    return f"{_MONTHS[int(number) - 1]} {year}"


def question_for(event: Event) -> str:
    """The question ADR-0003 §3's template asks about this event."""
    if event.template == ADDED:
        return f"Why was `{event.qualified_name}` added to `{event.path}`?"
    return f"Why did `{event.qualified_name}` change in {month_label(event.month)}?"


def qid_for(event: Event) -> str:
    """`<template>-<sha[:12]>-<sha256(path::X)[:8]>`: the same candidate, the same id."""
    anchor = hashlib.sha256(f"{event.path}::{event.qualified_name}".encode()).hexdigest()
    return f"{event.template}-{event.sha[:12]}-{anchor[:8]}"


def required_evidence_for(event: Event, pr: int | None) -> GoldEvidence:
    """The commit, plus its PR when slice B2 linked one (ADR-0003 §4)."""
    return GoldEvidence(commit_shas=[event.sha], pr_numbers=[] if pr is None else [pr])


def record_for(
    event: Event,
    *,
    pr: int | None,
    split: Split,
    facts: LeakageFacts,
    corpus_commit: str,
    index_version: str,
) -> GoldenRecord:
    """One candidate as a golden record, carrying the leakage facts measured for it."""
    qid = qid_for(event)
    if facts.gold_symbol_df is None:
        raise ValueError(f"{qid}: a generated record needs its symbol's document frequency")
    return GoldenRecord(
        qid=qid,
        query_class=QUERY_CLASS[event.template],
        split=split,
        question=question_for(event),
        required_evidence=required_evidence_for(event, pr),
        supporting_evidence=GoldEvidence(
            symbol_anchors=[SymbolAnchor(path=event.path, qualified_name=event.qualified_name)]
        ),
        generation_strategy=GENERATION_STRATEGY,
        leakage=LeakageProbe(
            baseline_grep_r10=float(facts.grep_hit_at_k),
            baseline_bm25_r10=float(facts.bm25_hit_at_k),
            gold_symbol_df=facts.gold_symbol_df,
            grep_answers_at_rank_1=facts.grep_answers_at_rank_1,
            rare_tokens_shared_with_gold=facts.rare_tokens_shared_with_gold,
            probe_index_version=index_version,
            probe_corpus_commit=corpus_commit,
        ),
        corpus_commit=corpus_commit,
    )


def drop_reason(record: GoldenRecord) -> str | None:
    """Why ADR-0003 §5 drops this record, or None when it is retained.

    Defined through `passes_leakage_filter`, so the two cannot disagree. A dropped record
    gets one reason, checked in the filter's order.
    """
    if passes_leakage_filter(record):
        return None
    probe = record.leakage
    if (
        probe is None
        or probe.grep_answers_at_rank_1 is None
        or probe.rare_tokens_shared_with_gold is None
    ):
        return UNMEASURED
    return RANK_1 if probe.grep_answers_at_rank_1 else RARE_TOKEN

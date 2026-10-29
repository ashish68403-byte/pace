"""A candidate becomes the same golden record every time (ADR-0003 §3-§7)."""

from __future__ import annotations

import hashlib

import pytest

from provenance.eval.candidates import ADDED, CHANGED, Event
from provenance.eval.generator import (
    GENERATION_STRATEGY,
    RANK_1,
    RARE_TOKEN,
    UNMEASURED,
    drop_reason,
    month_label,
    qid_for,
    question_for,
    record_for,
)
from provenance.eval.leakage import LeakageFacts
from provenance.eval.schema import GoldenRecord, passes_leakage_filter

SHA = "1e2ad803f7" + "ab" * 15
PATH = "airflow-core/src/airflow/jobs/scheduler_job_runner.py"
NAME = "SchedulerJobRunner._find_zombies"
ADDED_EVENT = Event(ADDED, 7, PATH, NAME, SHA, 100, "2023-03")
CHANGED_EVENT = Event(CHANGED, 7, PATH, NAME, SHA, 100, "2023-03")
CLEAN = LeakageFacts(False, [], False, True, 4)


def record(event=ADDED_EVENT, *, pr=31234, facts=CLEAN, split="dev"):
    return record_for(
        event,
        pr=pr,
        split=split,
        facts=facts,
        corpus_commit="f" * 40,
        index_version="commit-message-v1:6810",
    )


def test_the_questions_follow_the_templates():
    assert question_for(ADDED_EVENT) == f"Why was `{NAME}` added to `{PATH}`?"
    assert question_for(CHANGED_EVENT) == f"Why did `{NAME}` change in March 2023?"


def test_month_names_are_english_whatever_the_locale():
    assert [month_label(f"2020-{m:02d}") for m in (1, 12)] == ["January 2020", "December 2020"]


def test_the_qid_is_deterministic_and_valid():
    anchor = hashlib.sha256(f"{PATH}::{NAME}".encode()).hexdigest()[:8]
    assert qid_for(ADDED_EVENT) == f"added-{SHA[:12]}-{anchor}"
    assert qid_for(CHANGED_EVENT) == f"changed-{SHA[:12]}-{anchor}"
    GoldenRecord.model_validate(record().model_dump())  # the qid passes the schema's pattern


def test_required_evidence_is_the_commit_and_its_pr():
    assert record().required_evidence.keys() == [("commit", SHA), ("pr", "31234")]
    assert record(pr=None).required_evidence.keys() == [("commit", SHA)]


def test_the_supporting_anchor_is_todays_path_and_the_symbol():
    [anchor] = record().supporting_evidence.symbol_anchors
    assert (anchor.path, anchor.qualified_name) == (PATH, NAME)


def test_the_record_carries_its_class_split_strategy_and_facts():
    rec = record(facts=LeakageFacts(True, ["zombies"], True, False, 0), split="test")
    assert (rec.query_class, record(CHANGED_EVENT).query_class) == ("provenance", "archaeology")
    assert rec.split == "test"
    assert rec.generation_strategy == GENERATION_STRATEGY == "ast_diff_v0"
    assert rec.leakage is not None
    assert rec.leakage.model_dump(exclude_none=True) == {
        "baseline_grep_r10": 1.0,
        "baseline_bm25_r10": 0.0,
        "gold_symbol_df": 0,
        "grep_answers_at_rank_1": True,
        "rare_tokens_shared_with_gold": ["zombies"],
        "probe_index_version": "commit-message-v1:6810",
        "probe_corpus_commit": "f" * 40,
    }
    assert rec.corpus_commit == "f" * 40


def test_a_record_without_its_symbols_document_frequency_is_refused():
    with pytest.raises(ValueError, match="document frequency"):
        record(facts=LeakageFacts(False, [], False, False, None))


def test_each_dropped_record_has_exactly_one_reason():
    assert drop_reason(record()) is None
    assert drop_reason(record(facts=LeakageFacts(True, ["zombies"], True, True, 1))) == RANK_1
    assert drop_reason(record(facts=LeakageFacts(False, ["zombies"], False, True, 1))) == (
        RARE_TOKEN
    )
    clean = record()
    assert clean.leakage is not None
    unmeasured = clean.model_copy(
        update={"leakage": clean.leakage.model_copy(update={"grep_answers_at_rank_1": None})}
    )
    assert drop_reason(unmeasured) == UNMEASURED
    for rec in (clean, unmeasured):
        assert (drop_reason(rec) is None) == passes_leakage_filter(rec)

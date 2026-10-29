"""The golden record's split (ADR-0003 §7, review finding 2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from provenance.eval.schema import (
    DEV_FRACTION,
    GoldenRecord,
    assign_splits,
    dev_share_by_class,
    group_key,
    passes_leakage_filter,
)

SHA = "ab" * 20


def payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "qid": "t-1",
        "query_class": "provenance",
        "question": "Why was `X` added to `pkg/x.py`?",
        "required_evidence": {"commit_shas": [SHA]},
        "generation_strategy": "handwritten_test",
        "split": "dev",
    }
    base.update(overrides)
    return base


def test_a_record_must_say_which_split_it_is_in() -> None:
    GoldenRecord.model_validate(payload())
    without = payload()
    del without["split"]
    with pytest.raises(ValidationError, match="split"):
        GoldenRecord.model_validate(without)


def test_only_dev_and_test_exist() -> None:
    with pytest.raises(ValidationError):
        GoldenRecord.model_validate(payload(split="train"))


def test_the_pr_is_the_group_when_there_is_one() -> None:
    assert group_key(SHA, 47798) == "pr:47798"
    assert group_key(SHA, None) == f"commit:{SHA}"


def test_assignment_ignores_run_and_order() -> None:
    groups = [f"pr:{n}" for n in range(200)]
    first = assign_splits(groups)
    assert assign_splits(groups) == first
    assert assign_splits(reversed(groups)) == first


def test_each_group_is_decided_once() -> None:
    # Three records from PR 1 and one from PR 2: two decisions, not four. A record's
    # split is looked up by its group, so one PR's records cannot straddle the split.
    assert set(assign_splits(["pr:1", "pr:1", "pr:2", "pr:1"])) == {"pr:1", "pr:2"}


def test_about_sixty_percent_land_in_dev() -> None:
    splits = assign_splits(f"pr:{n}" for n in range(1000))
    share = sum(1 for s in splits.values() if s == "dev") / len(splits)
    assert DEV_FRACTION == 0.6
    assert 0.55 <= share <= 0.65  # 0.603 with the committed seed


def test_a_different_seed_reshuffles() -> None:
    groups = [f"pr:{n}" for n in range(200)]
    assert assign_splits(groups, seed="another-seed") != assign_splits(groups)


def test_the_dev_share_is_reported_per_class() -> None:
    records = [
        GoldenRecord.model_validate(payload(qid=f"p-{i}", split="dev" if i < 3 else "test"))
        for i in range(4)
    ]
    records.append(
        GoldenRecord.model_validate(payload(qid="a-1", query_class="archaeology", split="test"))
    )
    assert dev_share_by_class(records) == {"provenance": 0.75, "archaeology": 0.0}


def generated(**leakage: object) -> GoldenRecord:
    """A generated record whose leakage facts are clean unless overridden."""
    facts: dict[str, object] = {
        "baseline_grep_r10": 0.0,
        "baseline_bm25_r10": 0.0,
        "gold_symbol_df": 0,
        "grep_answers_at_rank_1": False,
        "rare_tokens_shared_with_gold": [],
    }
    facts.update(leakage)
    return GoldenRecord.model_validate(payload(generation_strategy="ast_diff_v0", leakage=facts))


def test_a_clean_generated_record_is_retained() -> None:
    assert passes_leakage_filter(generated())


def test_a_rank_1_grep_answer_or_a_shared_rare_token_drops_a_record() -> None:
    assert not passes_leakage_filter(generated(grep_answers_at_rank_1=True))
    assert not passes_leakage_filter(generated(rare_tokens_shared_with_gold=["zombies"]))


def test_missing_facts_fail_closed() -> None:
    assert not passes_leakage_filter(generated(grep_answers_at_rank_1=None))
    assert not passes_leakage_filter(generated(rare_tokens_shared_with_gold=None))


def test_grep_recall_alone_no_longer_decides() -> None:
    # The pre-ADR-0003 rule kept a record at or below 0.35 grep recall. A record grep
    # finds in its top 10 but not at rank 1, sharing no rare token, is now retained.
    assert passes_leakage_filter(generated(baseline_grep_r10=1.0))


def test_hand_written_records_are_always_retained() -> None:
    assert passes_leakage_filter(GoldenRecord.model_validate(payload()))

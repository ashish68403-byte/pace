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

"""Relevance units, end to end without a database (ADR-0003 §4).

The resolver turns each resolved required evidence key into one unit; the runner scores
the share of units with any member in the top k, and records the units' shape.
"""

from __future__ import annotations

from typing import Any

from provenance.eval.anchors import ResolvedAnchors, resolve_anchors
from provenance.eval.runner import PER_QUERY_COLUMNS, _score_one
from provenance.eval.schema import GoldenRecord

SHA = "cd" * 20


def record() -> GoldenRecord:
    return GoldenRecord.model_validate(
        {
            "qid": "u-1",
            "query_class": "provenance",
            "split": "dev",
            "question": "Why was `X` added to `pkg/x.py`?",
            "required_evidence": {"commit_shas": [SHA], "pr_numbers": [7]},
            "generation_strategy": "handwritten_test",
        }
    )


class FakeCursor:
    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[str, str, str]]:
        return self._rows


class FakeConn:
    """Answers the one chunk_evidence query resolve_anchors makes for commit and PR keys.

    Rows are (artifact_kind, evidence_key, chunk_id), the shape `_resolve_evidence` reads;
    with no symbol anchors in the record, no other query is made.
    """

    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        self.rows = rows

    def execute(self, query: str, params: Any = None) -> FakeCursor:
        return FakeCursor(self.rows)


def test_each_resolved_required_key_becomes_one_unit() -> None:
    conn = FakeConn(
        [
            ("commit", SHA, "msg-1"),
            ("pull_request", "7", "msg-1"),
            ("pull_request", "7", "pr-desc"),
        ]
    )
    resolved = resolve_anchors(record(), conn, repo_id=1)
    assert resolved.required_units == {
        ("commit", SHA): {"msg-1"},
        ("pr", "7"): {"msg-1", "pr-desc"},
    }
    assert resolved.required_chunk_ids == {"msg-1", "pr-desc"}


def test_an_unresolved_key_is_not_a_unit() -> None:
    resolved = resolve_anchors(record(), FakeConn([("commit", SHA, "msg-1")]), repo_id=1)
    assert set(resolved.required_units) == {("commit", SHA)}
    assert resolved.unresolved_required == {("pr", "7")}


def test_the_runner_scores_units_and_records_their_shape() -> None:
    pr_docs = {f"pr-doc-{i}" for i in range(30)}
    resolved = ResolvedAnchors(
        qid="u-1", required_chunk_ids=set(pr_docs), required_units={("pr", "7"): pr_docs}
    )
    row = _score_one(
        record(),
        resolved,
        ["noise", "pr-doc-3"],
        k=10,
        latency_ms=1.0,
        refused=False,
        citations=["pr-doc-3"],
    )
    assert row["recall_at_k"] == 1.0
    assert (row["n_units"], row["max_parts_per_unit"]) == (1, 30)
    assert {"n_units", "max_parts_per_unit"} <= set(PER_QUERY_COLUMNS)

"""Relevance units, end to end without a database (ADR-0003 §4).

The resolver turns each resolved required evidence key into one unit; the runner scores
the share of units with any member in the top k, and records the units' shape.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from provenance.eval.anchors import ResolvedAnchors, resolve_anchors
from provenance.eval.runner import PER_QUERY_COLUMNS, _score_one, aggregate
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


def generated(df: int) -> GoldenRecord:
    return GoldenRecord.model_validate(
        {
            "qid": "g-1",
            "query_class": "archaeology",
            "split": "dev",
            "question": "Why did `X` change in March 2023?",
            "required_evidence": {"commit_shas": [SHA], "pr_numbers": [7]},
            "supporting_evidence": {
                "symbol_anchors": [{"path": "pkg/x.py", "qualified_name": "X"}]
            },
            "generation_strategy": "ast_diff_v0",
            "leakage": {
                "baseline_grep_r10": 0.0,
                "baseline_bm25_r10": 1.0,
                "gold_symbol_df": df,
                "grep_answers_at_rank_1": False,
                "rare_tokens_shared_with_gold": [],
            },
        }
    )


def test_a_row_carries_its_first_gold_rank_and_its_leakage_facts() -> None:
    change = {"msg", "pr"}
    resolved = ResolvedAnchors(
        qid="g-1",
        required_chunk_ids=set(change),
        required_units={("commit", SHA): change, ("pr", "7"): change},
    )
    row = _score_one(
        generated(0), resolved, ["x", "pr"], k=10, latency_ms=1.0, refused=False, citations=["pr"]
    )
    assert (row["first_gold_rank"], row["gold_symbol_df"], row["baseline_grep_r10"]) == (2, 0, 0.0)
    assert row["ndcg_at_k"] == pytest.approx(1 / math.log2(3))
    assert {"first_gold_rank", "gold_symbol_df", "baseline_grep_r10"} <= set(PER_QUERY_COLUMNS)


def test_thirty_documents_of_one_pr_count_once_in_ndcg() -> None:
    pr_docs = {f"pr-doc-{i}" for i in range(30)}
    resolved = ResolvedAnchors(
        qid="u-1", required_chunk_ids=set(pr_docs), required_units={("pr", "7"): pr_docs}
    )

    def score(retrieved: list[str]) -> dict[str, Any]:
        return _score_one(
            record(), resolved, retrieved, k=10, latency_ms=1.0, refused=False, citations=[]
        )

    padded, single = score(["noise", *sorted(pr_docs)[:9]]), score(["noise", "pr-doc-3"])
    assert padded["ndcg_at_k"] == pytest.approx(single["ndcg_at_k"])
    assert single["ndcg_at_k"] == pytest.approx(1 / math.log2(3))
    assert (padded["first_gold_rank"], single["gold_symbol_df"]) == (2, None)


def agg_row(qid: str, recall: float, df: int | None) -> dict[str, Any]:
    return {
        "qid": qid,
        "query_class": "archaeology",
        "k": 10,
        "recall_at_k": recall,
        "mrr": recall,
        "ndcg_at_k": recall,
        "citation_precision": recall,
        "refused": False,
        "expected_refusal": False,
        "latency_ms": 1.0,
        "unresolved_anchors": 0,
        "gold_symbol_df": df,
    }


def test_aggregate_breaks_recall_down_by_the_symbols_document_frequency() -> None:
    rows = [
        agg_row("a", 1.0, 0),
        agg_row("b", 0.0, 0),
        agg_row("c", 1.0, 3),
        agg_row("d", 1.0, None),
    ]
    per_df = aggregate(rows)["per_symbol_df"]
    assert (per_df["df_zero"]["n"], per_df["df_zero"]["recall@10"]) == (2, 0.5)
    assert (per_df["df_positive"]["n"], per_df["df_positive"]["recall@10"]) == (1, 1.0)
    assert per_df["unmeasured"] == 1

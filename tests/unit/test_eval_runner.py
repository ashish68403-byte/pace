"""``load_retriever`` must hand back an *instance*, never the class it was named after.

The distinction is invisible right up until something calls the protocol method. A
class satisfies ``hasattr(obj, "retrieve")`` through its own unbound function, so a
loader that tests only for that attribute concludes "already a retriever" and returns
the class object. The first ``retriever.retrieve(question, qvec, k)`` then binds
``question`` to ``self``, ``qvec`` to ``question``, ``k`` to ``qvec``, and dies on a
missing ``k``.

That is how the default spec shipped, and the default is ``NullRetriever`` -- which
means ``pace eval run`` had never scored a single query, on any machine, ever. The
eval harness is the thing Phase 0 exists to establish *before* the agent, so a harness
that cannot complete a run is worth a test of its own.

The three loader paths are covered together because they are one decision: a class and
a factory both need calling, an already-built singleton must not be called twice.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from provenance.eval import runner as runner_mod
from provenance.eval.metrics import mde
from provenance.eval.runner import (
    NullRetriever,
    compare_to_baseline,
    load_retriever,
    select_records,
)
from provenance.eval.schema import GoldenRecord


def test_class_spec_is_instantiated() -> None:
    retriever = load_retriever("provenance.eval.runner:NullRetriever")
    assert retriever is not NullRetriever, "returned the class, not an instance"
    assert isinstance(retriever, NullRetriever)


def test_loaded_retriever_accepts_the_protocol_signature() -> None:
    """The regression itself: three positional args, as ``_score_records`` calls it."""
    retriever = load_retriever("provenance.eval.runner:NullRetriever")
    assert list(retriever.retrieve("why is this code the way it is?", None, 10)) == []


def test_singleton_spec_is_returned_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    singleton = NullRetriever()
    monkeypatch.setattr(runner_mod, "_probe_singleton", singleton, raising=False)
    assert load_retriever("provenance.eval.runner:_probe_singleton") is singleton


def test_factory_spec_is_called(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "_probe_factory", NullRetriever, raising=False)
    retriever = load_retriever("provenance.eval.runner:_probe_factory")
    assert isinstance(retriever, NullRetriever)


def test_name_defaults_to_the_attribute_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    class Unnamed:
        def retrieve(self, question: str, qvec: object, k: int) -> list[str]:
            return []

    monkeypatch.setattr(runner_mod, "_probe_unnamed", Unnamed, raising=False)
    assert load_retriever("provenance.eval.runner:_probe_unnamed").name == "_probe_unnamed"


def test_a_spec_without_retrieve_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "_probe_not_a_retriever", 42, raising=False)
    with pytest.raises(Exception, match="does not provide"):
        load_retriever("provenance.eval.runner:_probe_not_a_retriever")


# ------------------------------------------------------------------ slice B5a: the split, D


def generated(qid: str, split: str, *, rank_1: bool = False) -> GoldenRecord:
    return GoldenRecord.model_validate(
        {
            "qid": qid,
            "query_class": "archaeology",
            "split": split,
            "question": f"Why did `f_{qid}` change in March 2023?",
            "required_evidence": {"commit_shas": ["ab" * 20]},
            "generation_strategy": "ast_diff_v0",
            "leakage": {
                "baseline_grep_r10": 0.0,
                "baseline_bm25_r10": 0.0,
                "gold_symbol_df": 1,
                "grep_answers_at_rank_1": rank_1,
                "rare_tokens_shared_with_gold": [],
            },
        }
    )


def test_one_split_and_retained_records_only() -> None:
    records = [generated("a", "dev"), generated("b", "test"), generated("c", "dev", rank_1=True)]
    kept, skipped = select_records(records, "dev")
    assert [r.qid for r in kept] == ["a"]
    assert skipped == {"other_split": 1, "dropped_by_leakage_filter": 1}
    assert [r.qid for r in select_records(records, "all")[0]] == ["a", "b"]
    with pytest.raises(typer.BadParameter):
        select_records(records, "train")


def test_update_baseline_refuses_any_split_that_includes_test(tmp_path) -> None:
    golden = tmp_path / "none.jsonl"
    for split in ("test", "all"):
        args = ["run", "--golden", str(golden), "--no-db", "--no-invariants"]
        result = CliRunner().invoke(runner_mod.app, [*args, "--split", split, "--update-baseline"])
        assert result.exit_code == 2, result.output
        assert "reads dev only" in result.output


def test_the_paired_comparison_names_d_and_the_mde() -> None:
    run = {"recall@10": 0.5, "per_query": {f"q{i}": float(i % 2) for i in range(10)}}
    base = {
        "n_queries": 10,
        "recall@10": 0.5,
        "per_query": {f"q{i}": float(i < 5) for i in range(10)},
    }
    cmp = compare_to_baseline(run, base, model="m", index_version=None)
    info = next(m for m in cmp.messages if "paired delta" in m)
    # 0,1,0,1,... against 1,1,1,1,1,0,0,0,0,0: they differ on q0, q2, q4, q5, q7 and q9
    assert "D=6 of 10" in info and f"MDE={mde(10, 0.6):.4f}" in info


def test_identical_runs_say_no_difference_is_measurable() -> None:
    per_query = {f"q{i}": 1.0 for i in range(5)}
    cmp = compare_to_baseline(
        {"recall@10": 1.0, "per_query": per_query},
        {"n_queries": 5, "recall@10": 1.0, "per_query": per_query},
        model="m",
        index_version=None,
    )
    assert any("D=0 of 5" in m and "no difference is measurable" in m for m in cmp.messages)

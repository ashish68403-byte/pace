"""Slice D1: the unanswerable questions' rules (ADR-0006 §2), on hand-made questions."""

from __future__ import annotations

import pytest

from provenance.eval.schema import (
    GoldenRecord,
    GoldEvidence,
    QueryClass,
    SymbolAnchor,
    UnanswerableStrategy,
)
from provenance.eval.unanswerable import (
    AFTER_THE_PIN,
    UnanswerableRefused,
    choose_s3,
    choose_s4,
    choose_s5,
    function_of,
    month_of,
    ranked,
    s3,
    s4,
    s5,
    splice,
)

SHA = "ab" * 20
PIN = "cd" * 20
FILE = "pkg/m.py"


def record(
    qid: str, name: str, *, month: str | None = None, split: str = "dev", path: str = FILE
) -> GoldenRecord:
    """A retained question about `name`: "changed" in `month`, or "added" without one."""
    question = (
        f"Why did `{name}` change in {month}?" if month else f"Why was `{name}` added to `{path}`?"
    )
    return GoldenRecord(
        qid=qid,
        query_class=QueryClass.ARCHAEOLOGY if month else QueryClass.PROVENANCE,
        split=split,
        question=question,
        required_evidence=GoldEvidence(commit_shas=[SHA], pr_numbers=[7]),
        supporting_evidence=GoldEvidence(
            symbol_anchors=[SymbolAnchor(path=path, qualified_name=name)]
        ),
        generation_strategy="handwritten_d1_rules",
        corpus_commit=PIN,
    )


def nowhere(short: str) -> bool:
    """A pool that mentions no name."""
    return False


def test_the_splice() -> None:
    assert splice("_get_dag_runs", "update_state") == "_get_dag_state"
    assert splice("execute", "run") == "execute_run"
    assert splice("plus", "multiply") == "plus_multiply"
    assert splice("zero_b", "zero_a") == "zero_a"
    assert splice("_", "update_state") is None
    assert splice("execute", "__") is None


def test_each_kind_keeps_what_adr_0006_says() -> None:
    changed = record("q1", "Box.grow", month="March 2023")
    function = [SymbolAnchor(path=FILE, qualified_name="Box.grow")]
    held = s3(changed)
    assert (held.qid, held.question, held.split) == ("s3-q1", changed.question, "dev")
    assert held.held_out == changed.required_evidence and held.required_evidence.is_empty()
    assert held.supporting_evidence.symbol_anchors == function
    later = s4(changed)
    assert later.question in {f"Why did `Box.grow` change in {month}?" for month in AFTER_THE_PIN}
    assert later.supporting_evidence.symbol_anchors == function
    named = s5(changed, "Box.grow_all")
    assert named.question == "Why did `Box.grow_all` change in March 2023?"
    assert named.supporting_evidence.symbol_anchors == [SymbolAnchor(path=FILE)]
    added = s5(record("q2", "helper"), "helper_a")
    assert added.question == f"Why was `helper_a` added to `{FILE}`?"
    assert [negative.unanswerable_strategy for negative in (held, later, named)] == [
        UnanswerableStrategy.S3_HELD_OUT,
        UnanswerableStrategy.S4_TEMPORAL,
        UnanswerableStrategy.S5_NONEXISTENT,
    ]
    for negative in (held, later, named):
        assert negative.query_class is QueryClass.UNANSWERABLE
        assert (negative.leakage, negative.corpus_commit) == (None, PIN)


def test_a_question_the_rules_cannot_read_is_refused() -> None:
    odd = record("q1", "grow").model_copy(update={"question": "What does `grow` do today?"})
    with pytest.raises(UnanswerableRefused, match="two templates"):
        month_of(odd)
    anchors = [SymbolAnchor(path=FILE, qualified_name=name) for name in ("grow", "shrink")]
    two = record("q2", "grow").model_copy(
        update={"supporting_evidence": GoldEvidence(symbol_anchors=anchors)}
    )
    with pytest.raises(UnanswerableRefused, match="2 functions, not one"):
        function_of(two)


def test_s3_takes_the_first_questions_by_hash() -> None:
    records = [record(f"q{index}", f"f{index}") for index in range(6)]
    held, tally = choose_s3(records, 2)
    assert [r.qid for r in held] == [f"s3-{r.qid}" for r in ranked(records, "s3")[:2]]
    assert tally == {"read": 2}


def test_s4_asks_about_each_function_once_in_the_file() -> None:
    records = [record(f"q{index}", f"f{index % 3}") for index in range(6)]
    later, _ = choose_s4(records, 3, used=set())
    assert sorted(function_of(r)[1] for r in later) == ["f0", "f1", "f2"]
    short, _ = choose_s4(records, 4, used=set())
    assert len(short) == 3  # three functions only: the build refuses the shortfall
    functions: set[tuple[str, str]] = set()
    dev, _ = choose_s4([record("d1", "grow"), record("d2", "shrink")], 1, used=functions)
    test, tally = choose_s4(
        [record("t1", "grow", split="test"), record("t2", "shrink", split="test")],
        2,
        used=functions,
    )
    assert len(test) == 1 and {function_of(r) for r in dev}.isdisjoint(map(function_of, test))
    assert tally == {"read": 2}


def test_s5_rejects_a_name_lineage_has_seen_or_the_pool_mentions() -> None:
    chosen, tally = choose_s5(
        [record("q1", "zero_b")],
        1,
        partners={FILE: ["zero_a", "zero_b", "helper"]},
        seen={"zero_a", "zero_b", "helper"},
        used=set(),
        in_pool=lambda short: short == "zero_helper",
    )
    assert chosen == []
    assert tally == {
        "read": 1,
        "no_parts": 0,
        "tried": 2,
        "seen_by_lineage": 1,
        "already_used": 0,
        "found_in_the_pool": 1,
    }


def test_s5_uses_a_name_once_in_the_file() -> None:
    partners, seen = {FILE: ["helper", "zero_a"]}, {"helper", "zero_a"}
    names: set[str] = set()
    dev, tally = choose_s5(
        [record("d1", "helper"), record("d2", "helper")],
        2,
        partners=partners,
        seen=seen,
        used=names,
        in_pool=nowhere,
    )
    assert [r.question for r in dev] == [f"Why was `helper_a` added to `{FILE}`?"]
    assert (tally["read"], tally["tried"], tally["already_used"]) == (2, 2, 1)
    test, tally = choose_s5(
        [record("t1", "helper", split="test")],
        1,
        partners=partners,
        seen=seen,
        used=names,
        in_pool=nowhere,
    )
    assert test == [] and tally["already_used"] == 1 and names == {"helper_a"}


def test_a_method_keeps_its_class_and_a_name_of_underscores_gives_nothing() -> None:
    partners = {FILE: ["Box.grow", "Box.shrink_all", "Box._"]}
    seen = {"Box.grow", "Box.shrink_all", "Box._"}
    chosen, _ = choose_s5(
        [record("q1", "Box.grow")], 1, partners=partners, seen=seen, used=set(), in_pool=nowhere
    )
    assert [r.question for r in chosen] == [f"Why was `Box.grow_all` added to `{FILE}`?"]
    nothing, tally = choose_s5(
        [record("q2", "Box._")], 1, partners=partners, seen=seen, used=set(), in_pool=nowhere
    )
    assert nothing == [] and (tally["no_parts"], tally["tried"]) == (2, 0)


def test_the_choice_does_not_depend_on_the_input_order() -> None:
    records = [record(f"q{index}", f"f{index}") for index in range(5)]
    assert ranked(records, "s3") == ranked(records[::-1], "s3")
    assert choose_s4(records, 3, used=set()) == choose_s4(records[::-1], 3, used=set())

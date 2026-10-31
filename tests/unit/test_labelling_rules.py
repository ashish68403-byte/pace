"""ADR-0004 §4's two-step labelling, against scripted answers: no database, no terminal."""

from __future__ import annotations

from datetime import date

from provenance.eval.gold_validity import LabelRecord, SampledRecord
from provenance.eval.labelling import GoldView, Screen, label_one, run_session

TODAY = date(2026, 11, 1)


def view(qid: str = "q1", *, linked: bool = True) -> GoldView:
    return GoldView(
        qid=qid,
        stratum="archaeology-pr" if linked else "archaeology-commit",
        question="Why did `helper` change in January 2020?",
        path="core/src/pkg/alpha.py",
        qualified_name="helper",
        commit="ab" * 20,
        committed_on="2020-01-07",
        message="edit alpha after move (#14)",
        change="-    return 'one'\n+    return 'three'",
        pr=14 if linked else None,
        description="PR 14\n\nHelper says three now." if linked else None,
        comments=("Three, because the scheduler changed.",) if linked else (),
    )


class Script:
    """Answers prompts in order, and keeps everything shown."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.lines: list[str] = []
        self.screen = Screen(ask=self._ask, show=self.lines.append)

    def _ask(self, prompt: str) -> str:
        self.lines.append(prompt)
        return self.answers.pop(0)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def test_a_first_rationale_asks_once_and_stands_for_both():
    script = Script("1", "")
    record = label_one(script.screen, view(), "label 1/1", TODAY)
    assert (record.message_label, record.label) == ("rationale", "rationale")
    assert record.read_comments is False and record.note is None
    assert "Helper says three now." not in script.text


def test_after_mentions_the_final_label_cannot_claim_less():
    script = Script("2", "3", "1", "PR explains it")
    record = label_one(script.screen, view(), "label 1/1", TODAY)
    assert (record.message_label, record.label) == ("mentions", "rationale")
    assert record.note == "PR explains it"
    assert "Helper says three now." in script.text
    assert "type one of: 1, 2, d, c" in script.text  # the 3 was refused


def test_a_commit_only_record_asks_once():
    script = Script("3", "")
    record = label_one(script.screen, view(linked=False), "label 1/1", TODAY)
    assert (record.message_label, record.label) == ("unrelated", "unrelated")
    assert record.read_comments is None


def test_d_and_c_show_text_and_are_recorded():
    script = Script("d", "2", "c", "2", "")
    record = label_one(script.screen, view(), "label 1/1", TODAY)
    assert (record.read_change, record.read_comments) == (True, True)
    assert "+    return 'three'" in script.text
    assert "Three, because the scheduler changed." in script.text


def test_comments_are_not_offered_before_the_pr_is_shown():
    script = Script("c", "1", "")
    record = label_one(script.screen, view(), "label 1/1", TODAY)
    assert record.read_comments is False
    assert "Three, because the scheduler changed." not in script.text


def test_invalid_input_asks_again():
    script = Script("x", "", "4", "1", "")
    assert label_one(script.screen, view(), "label 1/1", TODAY).label == "rationale"


def items(*qids: str) -> list[SampledRecord]:
    return [SampledRecord(q, "archaeology-pr", i, None, "h") for i, q in enumerate(qids, 1)]


def test_q_writes_nothing_and_the_next_session_starts_there():
    written: list[LabelRecord] = []
    first = Script("1", "", "q")
    run_session(
        items("a", "b"),
        set(),
        view=lambda item: view(item.qid),
        screen=first.screen,
        today=TODAY,
        write=written.append,
        title="label",
    )
    assert [r.qid for r in written] == ["a"]
    second = Script("2", "2", "")
    run_session(
        items("a", "b"),
        {"a"},
        view=lambda item: view(item.qid),
        screen=second.screen,
        today=TODAY,
        write=written.append,
        title="label",
    )
    assert [r.qid for r in written] == ["a", "b"]
    assert "[label 2/2]" in second.text


def test_a_relabel_session_shows_nothing_of_the_first_pass():
    first_pass = LabelRecord(
        "a",
        "archaeology-pr",
        "mentions",
        "rationale",
        "only the first pass wrote this",
        TODAY,
        False,
        True,
    )
    written: list[LabelRecord] = []
    script = Script("2", "1", "")
    run_session(
        items("a"),
        set(),
        view=lambda item: view(item.qid),
        screen=script.screen,
        today=TODAY,
        write=written.append,
        title="relabel",
    )
    assert first_pass.note is not None and first_pass.note not in script.text
    assert [r.qid for r in written] == ["a"]

"""Slice C1: the pre-registration's numbers and words in code, checked against the file."""

from __future__ import annotations

from pathlib import Path

from provenance.eval import preregistration as prereg

DOCUMENT = Path(__file__).resolve().parents[2] / "docs" / "PREREGISTRATION.md"


def test_the_thresholds_in_code_are_the_ones_committed_in_prose():
    assert prereg.read_thresholds(DOCUMENT) == prereg.THRESHOLDS
    assert prereg.THRESHOLDS == {
        "COVERAGE_MIN_GAIN": 0.05,
        "IDENTITY_MIN_F1_GAIN": 0.05,
        "AGREEMENT_MIN_F1": 0.90,
        "LEXICAL_CEILING": 0.35,
        "REFUSAL_MIN_GAIN": 0.05,
    }


def test_a_table_edited_to_disagree_is_caught(tmp_path):
    edited = tmp_path / "PREREGISTRATION.md"
    text = DOCUMENT.read_text(encoding="utf-8").replace("| 0.35 |", "| 0.40 |")
    edited.write_text(text, encoding="utf-8")
    assert prereg.read_thresholds(edited) != prereg.THRESHOLDS


def test_coverage_holds_only_at_the_minimum_gain_with_an_interval_above_zero():
    failed = prereg.FAILURE_WORDS["coverage"]
    assert prereg.coverage(0.05, 0.01) == prereg.HOLDS
    assert prereg.coverage(0.049, 0.01) == failed
    assert prereg.coverage(0.08, 0.0) == failed


def test_selection_returns_the_three_fixed_readings():
    assert prereg.selection(0.01, 0.03) == prereg.SELECTION_ABOVE
    assert prereg.selection(-0.01, 0.02) == prereg.SELECTION_CONTAINS
    assert prereg.selection(-0.03, -0.01) == prereg.SELECTION_BELOW


def test_identity_and_lexical_hold_and_fail_at_their_thresholds():
    assert prereg.identity(0.05, 0.001) == prereg.HOLDS
    assert prereg.identity(0.04, 0.01) == prereg.FAILURE_WORDS["identity"]
    assert prereg.identity(0.10, -0.01) == prereg.FAILURE_WORDS["identity"]
    assert prereg.lexical(0.349) == prereg.HOLDS
    assert prereg.lexical(0.35) == prereg.FAILURE_WORDS["lexical"]


def test_every_reading_and_failure_sentence_is_written_in_the_preregistration():
    text = " ".join(DOCUMENT.read_text(encoding="utf-8").split())  # lines wrap mid-sentence
    readings = (prereg.SELECTION_ABOVE, prereg.SELECTION_CONTAINS, prereg.SELECTION_BELOW)
    for words in (*readings, *prereg.FAILURE_WORDS.values()):
        assert words in text, words

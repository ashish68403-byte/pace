"""Slice B5b: an identifier in a question is searched by its parts, not as a phrase.

Measured 2026-11-08 on pace: pg_search's parser reads `_schedule_dag_run` as the phrase
"schedule dag run" (22 documents; the three words, loose, match 12,093). Written out as its
parts plus its joined form, a 600-record dev sample's hits@10 went from 24 to 91.
"""

from __future__ import annotations

import pytest

from provenance.parse import chunker
from provenance.retrieve import lexical
from provenance.retrieve.lexical import Bm25Retriever, _loosen, _query_forms


@pytest.mark.parametrize(
    ("query", "written_out"),
    [
        ("_schedule_dag_run", "schedule dag run scheduledagrun"),
        (
            "TaskInstance.refresh_from_db",
            "task instance taskinstance refresh from db refreshfromdb",
        ),
        ("camelCaseName", "camel case name camelcasename"),
        ("s3_hook", "s3 hook s3hook"),
    ],
)
def test_an_identifier_becomes_its_parts_and_its_joined_form(query, written_out):
    assert _loosen(query) == written_out


@pytest.mark.parametrize("query", ["alpha", "2020", "Why did the scheduler retry", "__init__"])
def test_single_part_words_are_left_alone(query):
    assert _loosen(query) == query


def test_a_generated_question_is_written_out_after_it_is_sanitised():
    question = "Why did `TaskInstance.refresh_from_db` change in May 2021?"
    exact, searched = _query_forms(question, "loose")
    assert exact == "Why did TaskInstance.refresh_from_db change in May 2021"
    assert searched == (
        "Why did task instance taskinstance refresh from db refreshfromdb change in May 2021"
    )


def test_the_qualified_name_field_still_gets_a_one_word_query_whole():
    exact, searched = _query_forms("_normalise_url", "loose")
    assert (exact, searched) == ("_normalise_url", "normalise url normaliseurl")
    assert " " not in exact  # so lexical_search still asks the literal field


def test_phrase_mode_searches_exactly_what_it_did_before_b5b():
    question = "Why did `TaskInstance.refresh_from_db` change in May 2021?"
    exact, searched = _query_forms(question, "phrase")
    assert searched == exact == lexical._sanitise(question)


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="identifiers must be one of"):
        _query_forms("alpha", "regex")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="identifiers must be one of"):
        Bm25Retriever(1, identifiers="regex")  # type: ignore[arg-type]


def test_the_query_finds_identifiers_with_the_pattern_the_index_used():
    assert lexical._IDENTIFIER.pattern == chunker._IDENT_RE.pattern


def test_the_retriever_names_its_row_by_mode():
    names = (Bm25Retriever(1).name, Bm25Retriever(1, identifiers="phrase").name)
    assert names == ("bm25", "bm25_phrase")

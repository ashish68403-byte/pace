"""pg_search's query parser rejects characters that real questions contain (slice B5a).

Measured on 2026-11-07 against the real index: each character below raised "could not
parse query string" inside `content @@@ ...`. The backtick and the apostrophe were missing
from the sanitiser, so every generated question -- they all wrap the symbol in backticks
-- failed in the production BM25 row, and so would "why doesn't X retry?".
"""

from __future__ import annotations

import pytest

from provenance.retrieve.lexical import _sanitise


@pytest.mark.parametrize("char", ["`", "'", ":", "(", ")", "^", '"', "[", "]", "{", "}"])
def test_every_character_the_parser_rejects_is_blanked(char: str) -> None:
    assert char not in _sanitise(f"why did alpha{char}beta change")


def test_a_generated_question_keeps_its_words() -> None:
    words = _sanitise("Why did `alpha` change in January 2020?").split()
    assert words == ["Why", "did", "alpha", "change", "in", "January", "2020"]


def test_an_apostrophe_in_plain_english_is_blanked() -> None:
    assert _sanitise("why doesn't the scheduler retry") == "why doesn t the scheduler retry"

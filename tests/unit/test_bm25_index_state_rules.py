"""Slice B5b: the ablation records whether BM25 was measured on a clean index.

Found 2026-11-08: pg_search scores with term statistics over every document in its segments,
including rows deleted but not yet vacuumed and vacuumed ones not yet merged away. The index
then held 71,253 documents and 2,162 deletions for 69,769 live rows, all left by test runs, and
12 of 4,206 BM25 ranks had moved since slice B5a with no code change.
"""

from __future__ import annotations

from provenance.retrieve.lexical import describe_index_state


def test_an_index_is_clean_only_when_it_holds_exactly_the_live_rows():
    assert describe_index_state(69769, 0, 69769) == {
        "documents": 69769,
        "deleted": 0,
        "rows": 69769,
        "clean": True,
    }
    assert describe_index_state(71253, 2162, 69769)["clean"] is False  # measured 2026-11-08
    assert describe_index_state(69769, 5, 69769)["clean"] is False
    assert describe_index_state(70000, 0, 69769)["clean"] is False

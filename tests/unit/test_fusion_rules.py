"""Slice B5b: the fused row follows tasks/eval-harness/007's settings, fixed in advance."""

from __future__ import annotations

from provenance.retrieve.fusion import DEFAULT_RRF_K, FUSION_DEPTH, RrfRetriever


class Source:
    """A ranked source that counts how often fusion asks it."""

    def __init__(self, ranking):
        self.ranking = list(ranking)
        self.calls = 0

    def ranked(self, question, qvec):
        self.calls += 1
        return self.ranking


def test_the_settings_are_the_ones_the_spec_fixed():
    assert (DEFAULT_RRF_K, FUSION_DEPTH) == (60, 100)
    assert RrfRetriever(Source([]), Source([])).k == 60


def test_a_document_in_both_lists_outranks_one_at_the_same_rank_in_a_single_list():
    rrf = RrfRetriever(Source(["a", "b"]), Source(["c", "a"]))
    # a: 1/61 + 1/62; c: 1/61; b: 1/62
    assert rrf.retrieve("q", None, 10) == ["a", "c", "b"]


def test_ties_break_on_chunk_id():
    assert RrfRetriever(Source(["y"]), Source(["x"])).retrieve("q", None, 10) == ["x", "y"]


def test_the_fused_list_is_cut_at_k_and_reads_each_source_once():
    lexical = Source([f"l{i:03d}" for i in range(100)])
    dense = Source([f"d{i:03d}" for i in range(100)])
    assert len(RrfRetriever(lexical, dense).retrieve("q", None, 10)) == 10
    assert (lexical.calls, dense.calls) == (1, 1)


def test_built_bare_it_fuses_the_production_retrievers():
    rrf = RrfRetriever()
    assert (type(rrf.lexical).__name__, type(rrf.dense).__name__) == (
        "Bm25Retriever",
        "DenseRetriever",
    )

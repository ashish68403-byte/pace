"""Retrieval for PACE.

Hybrid retrieval over the chunk table: BM25 (pg_search) + HNSW k-NN (pgvector),
merged with reciprocal rank fusion, in front of a rule-based query router.

Why hybrid is not optional here: the corpus is code. Exact identifier matching
matters enormously ("where is ``_normalise_url`` defined") and dense retrieval
is weak at it -- a 384-dim embedding of an identifier is a blurry average of its
word pieces. Conversely BM25 alone cannot answer "why does this retry on 429".
Each half covers the other's failure mode; RRF merges them without needing their
scores to be commensurable.

WHAT IS DELIBERATELY MISSING: rerank.py.
A cross-encoder rerank stage would help, but measured on this box (i3-1115G4,
2 physical cores, no CUDA):

    bge-reranker-base       23.83 s for 50 candidates   (budget is 9 s p95)
    ms-marco-MiniLM-L6-v2    3.57 s at depth 50
    ms-marco-MiniLM-L6-v2    0.63 s at depth 10
    ms-marco-MiniLM-L6-v2   17.4 s cold model load

So bge-reranker-base is disqualified outright, and MiniLM only fits at depth 10
AND only if the model is already resident. The 17.4 s cold load means a reranker
must live in a warm process (a preloaded worker holding the ONNX session), never
be constructed per query. Until that process exists, adding rerank.py would just
be a way to blow the latency budget in a demo. The hole is documented, not filled.

Modules:
    embedder.py  Embedder protocol + Local / Frozen / Api implementations.
    lexical.py   BM25 over the pg_search index (split-token AND literal forms).
    dense.py     HNSW k-NN over halfvec, plus exact brute-force ground truth.
    fusion.py    Reciprocal rank fusion.
    router.py    Rule-based query routing (symbol lookup vs hybrid).
"""

from __future__ import annotations

from provenance.retrieve.dense import DenseResult, dense_search, exact_search, measured_recall
from provenance.retrieve.embedder import (
    ApiEmbedder,
    Embedder,
    FrozenEmbedder,
    FrozenEmbeddingMiss,
    LocalEmbedder,
    get_embedder,
)
from provenance.retrieve.fusion import FusedResult, reciprocal_rank_fusion
from provenance.retrieve.lexical import LexicalResult, lexical_search
from provenance.retrieve.router import Route, RouteKind, route_query

__all__ = [
    "ApiEmbedder",
    "DenseResult",
    "Embedder",
    "FrozenEmbedder",
    "FrozenEmbeddingMiss",
    "FusedResult",
    "LexicalResult",
    "LocalEmbedder",
    "Route",
    "RouteKind",
    "dense_search",
    "exact_search",
    "get_embedder",
    "lexical_search",
    "measured_recall",
    "reciprocal_rank_fusion",
    "route_query",
]

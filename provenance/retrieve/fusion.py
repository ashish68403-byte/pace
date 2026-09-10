"""Reciprocal rank fusion of the lexical and dense rank lists.

    RRF(d) = sum over systems s of  weight_s / (k + rank_s(d))

with rank 1-based and k defaulting to 60.

WHY RANK-BASED AND NOT SCORE-NORMALISED -- this is a guaranteed viva question,
so the answer lives here:

  * The two scores are not on the same scale and never will be. BM25 is an
    unbounded sum of IDF-weighted, length-normalised term contributions; cosine
    similarity is bounded in [-1, 1] and, for a normalised embedding model over
    a homogeneous corpus, is squashed into a narrow band near the top (0.72 vs
    0.68 can be the difference between the right chunk and a wrong one). There
    is no principled conversion between them.
  * Min-max normalising per query fixes the units but invents a distribution.
    It rescales relative to whatever happened to be in THAT query's candidate
    list, so one outlier changes every other document's score, and a query where
    every result is bad gets a 1.0 at the top exactly like a query where the
    first result is perfect. It manufactures confidence out of nothing.
  * z-scoring assumes the scores are roughly normal within a query. BM25 tails
    are long and cosine scores are bunched; neither is.
  * Ranks are what both systems actually agree on the meaning of: "this one is
    my best guess". RRF only ever asks that question, so it is invariant to any
    monotonic rescaling of either system's scores. Change the embedding model,
    swap ts_rank_cd for real BM25, add the literal boost -- the fusion code does
    not need retuning, only the retrieval it consumes changes.
  * Empirically (Cormack et al. 2009) RRF beats the score-combination methods it
    was compared against, without per-collection training.

The cost: RRF throws away magnitude. A document ranked 1 with an overwhelming
BM25 score fuses identically to one ranked 1 by a hair. k controls how much that
matters -- it is the "how much is rank 1 worth over rank 10" dial. Small k makes
the top ranks dominant and the fusion brittle to a single system's mistake;
large k flattens toward "appear in both lists at all". 60 is the paper's value
and the starting point; the real value is whatever the golden set picks, and
`tune_k` is here to pick it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = ["DEFAULT_RRF_K", "FusedResult", "HasChunkId", "reciprocal_rank_fusion", "tune_k"]


@runtime_checkable
class HasChunkId(Protocol):
    """LexicalResult, DenseResult, SymbolHit, FusedResult -- anything ranked.

    Fusion deliberately knows nothing else about a result: the moment it reads a
    score it stops being rank-based, and the argument in this module's docstring
    stops holding.
    """

    chunk_id: str


DEFAULT_RRF_K = 60


@dataclass(frozen=True, slots=True)
class FusedResult:
    chunk_id: str
    score: float
    rank: int  # 1-based, post-fusion
    # Per-system contributing rank, e.g. {"lexical": 3, "dense": 11}. Kept
    # because it is the first thing you want when a result looks wrong: it says
    # immediately whether this chunk came from one system or both.
    ranks: Mapping[str, int] = field(default_factory=dict)


def _rank_map(items: Sequence[HasChunkId | str]) -> dict[str, int]:
    """Turn a result list into chunk_id -> 1-based rank.

    Accepts anything with a .chunk_id (LexicalResult, DenseResult, FusedResult)
    or a bare sequence of ids. Explicit .rank fields are ignored in favour of
    position, so a caller that sliced a list cannot smuggle in stale ranks.
    """
    out: dict[str, int] = {}
    for i, item in enumerate(items, start=1):
        cid = item if isinstance(item, str) else item.chunk_id
        out.setdefault(cid, i)  # first occurrence wins
    return out


def reciprocal_rank_fusion(
    rank_lists: Mapping[str, Sequence[HasChunkId | str]],
    *,
    k: int = DEFAULT_RRF_K,
    weights: Mapping[str, float] | None = None,
    limit: int = 20,
) -> list[FusedResult]:
    """Fuse named rank lists.

        rank_lists = {"lexical": [...], "dense": [...]}

    Documents missing from a list contribute nothing from it -- NOT a penalty
    term. Absence from BM25 usually means "no lexical overlap", which for a
    paraphrased question is normal and must not be punished; the other system
    carrying a document alone is the case hybrid retrieval exists to serve.

    ``weights`` lets one system be trusted more (e.g. {"lexical": 1.2} once the
    router knows the query is identifier-shaped). Default is 1.0 each.

    Ties break on chunk_id so output is deterministic for the ingest gate.
    """
    if k < 1:
        raise ValueError("RRF k must be >= 1 (k=0 makes rank-1 infinite)")

    weights = weights or {}
    scores: dict[str, float] = {}
    contributing: dict[str, dict[str, int]] = {}

    for system, items in rank_lists.items():
        w = float(weights.get(system, 1.0))
        if w == 0.0:
            continue
        for cid, rank in _rank_map(items).items():
            scores[cid] = scores.get(cid, 0.0) + w / (k + rank)
            contributing.setdefault(cid, {})[system] = rank

    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [
        FusedResult(chunk_id=cid, score=score, rank=i, ranks=contributing[cid])
        for i, (cid, score) in enumerate(ordered, start=1)
    ]


def tune_k(
    per_query_lists: Sequence[Mapping[str, Sequence[HasChunkId | str]]],
    relevant: Sequence[set[str]],
    *,
    candidates: Sequence[int] = (10, 20, 30, 60, 100, 200),
    at: int = 10,
) -> list[tuple[int, float]]:
    """Grid-search k on the golden set. Returns [(k, recall@at)] sorted best first.

    Run this on the TRAINING split of the golden set and report the chosen k
    with the number it earned. Tuning k on the same queries you then report
    on is how a project reports its own overfitting as a result.
    """
    # Queries with no gold are SKIPPED, so they must not sit in the
    # denominator either. Dividing by every query instead scales all candidate
    # k values by the same constant -- which leaves the ARGMAX correct and the
    # reported recall silently too low, so the plot in the report disagrees with
    # the recall the same code prints elsewhere.
    scored = sum(1 for gold in relevant if gold)
    out: list[tuple[int, float]] = []
    for k in candidates:
        total = 0.0
        for lists, gold in zip(per_query_lists, relevant, strict=True):
            if not gold:
                continue
            fused = reciprocal_rank_fusion(lists, k=k, limit=at)
            total += len({r.chunk_id for r in fused} & gold) / len(gold)
        out.append((k, total / scored if scored else 0.0))
    return sorted(out, key=lambda kv: (-kv[1], kv[0]))

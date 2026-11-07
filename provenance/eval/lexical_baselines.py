"""The leakage probe's cheap baselines as runner retrievers (slice B5a, ADR-0003 §6).

`grep` and `bm25_ident` rank exactly as the verdict ranked them -- the probe's own
`PoolIndex`, only documents that matched, best first -- so the runner can be checked
against the verdict's numbers. Built alone, a retriever loads the pool on its first
query; `pace eval ablation` builds one index and gives it to both.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from provenance.config import settings
from provenance.eval.leakage import PoolIndex, load_pool_from_db, tokenize_identifier_aware


def pool_index_from_db() -> PoolIndex:
    """The probe's index over the stored pool of the configured repository."""
    from provenance.graph.db import connection
    from provenance.graph.repos import resolve_repo_id

    with connection() as conn:
        repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
        return PoolIndex(load_pool_from_db(conn, repo_id))


class _ProbeRetriever:
    name = "probe"

    def __init__(self, index: PoolIndex | None = None) -> None:
        self._index = index

    @property
    def index(self) -> PoolIndex:
        if self._index is None:
            self._index = pool_index_from_db()
        return self._index

    def scores(self, question: str) -> np.ndarray:
        raise NotImplementedError

    def retrieve(self, question: str, qvec: np.ndarray | None, k: int) -> Sequence[str]:
        return self.index.matched(self.scores(question), k)


class GrepRetriever(_ProbeRetriever):
    """Literal substring search: the probe's `grep` row."""

    name = "grep"

    def scores(self, question: str) -> np.ndarray:
        return self.index.grep.score(question)


class Bm25IdentRetriever(_ProbeRetriever):
    """Identifier-aware BM25: the probe's `bm25_ident` row, the reference every lift is
    quoted against (ADR-0003 §6)."""

    name = "bm25_ident"

    def scores(self, question: str) -> np.ndarray:
        return self.index.bm25_ident.score(tokenize_identifier_aware(question))

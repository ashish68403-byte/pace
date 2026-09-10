"""PACE evaluation harness.

Built in week 1, *before* there is anything to evaluate. The point of building it
first is that every design decision downstream (chunker, index, retriever, agent)
gets measured against a gate that already exists, instead of a gate invented after
the numbers came in.

Three properties this package must never lose:

1. Gold evidence is keyed on *stable* identifiers (commit sha, PR/issue number,
   review-comment id, ``(path, qualified_name)``) and NEVER on ``chunk_id``.
   ``anchors.resolve_anchors()`` maps those to current chunk ids at scoring time,
   so a chunker change re-scores instead of invalidating the golden set.
2. The retrieval half of the CI gate makes ZERO network calls: query embeddings
   come from a committed ``.npz`` fixture and a cache miss raises. See
   ``runner.FrozenQueryEmbeddings``.
3. Aggregates are always recomputed from the per-query table that was just
   written to disk. A stored aggregate with no per-query rows behind it cannot be
   audited, and is therefore worthless in a dissertation.
"""

from __future__ import annotations

from provenance.eval.anchors import ResolvedAnchors, resolve_anchors
from provenance.eval.metrics import (
    BOOTSTRAP_B,
    BOOTSTRAP_SEED,
    CI,
    PairedCI,
    bootstrap_ci,
    citation_precision,
    format_metric,
    mrr,
    ndcg_at_k,
    paired_bootstrap,
    recall_at_k,
    refusal_precision,
    refusal_recall,
)
from provenance.eval.schema import (
    RETAINED_GREP_R10_MAX,
    GoldenRecord,
    GoldEvidence,
    LeakageProbe,
    QueryClass,
    SymbolAnchor,
    UnanswerableStrategy,
    load_golden_set,
    write_golden_set,
)

__all__ = [
    "BOOTSTRAP_B",
    "BOOTSTRAP_SEED",
    "CI",
    "GoldEvidence",
    "GoldenRecord",
    "LeakageProbe",
    "PairedCI",
    "QueryClass",
    "RETAINED_GREP_R10_MAX",
    "ResolvedAnchors",
    "SymbolAnchor",
    "UnanswerableStrategy",
    "bootstrap_ci",
    "citation_precision",
    "format_metric",
    "load_golden_set",
    "mrr",
    "ndcg_at_k",
    "paired_bootstrap",
    "recall_at_k",
    "refusal_precision",
    "refusal_recall",
    "resolve_anchors",
    "write_golden_set",
]

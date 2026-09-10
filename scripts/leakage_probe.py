#!/usr/bin/env python
"""Leakage probe: measure what *cheap* baselines score on the PACE golden set.

WHAT FAILURE THIS PREVENTS
--------------------------
A golden set built naively -- "pick a symbol, find the PR that changed it, write a
question that names the symbol" -- is not a retrieval benchmark. It is a primary-key
lookup with a question mark on the end. Measured on a first-pass Airflow golden set:

    grep recall@1  = 0.897
    grep recall@10 = 0.986      (n = 358 queries)

because the chosen gold symbol had a **median document frequency of 1**. One document
in the entire pool contained that string. Any system that can do `str.find` scores 0.99.
A neural retriever reporting 0.94 on that set is *worse than grep* and every number
downstream of it -- ablations, reranker lift, refusal precision -- means nothing.

So this probe is the gate that keeps every other number in the project meaningful.
It runs four baselines and reports them as four separate rows, so that "lift" is always
measured over the STRONGEST cheap baseline, never the most convenient weak one:

    1. grep          -- pure literal substring search over raw document text
    2. bm25_default  -- BM25 with the default word tokenizer (whole identifiers kept)
    3. bm25_ident    -- BM25 with the identifier-aware tokenizer, mirroring the real
                        index: pdb.source_code split parts + the underscore-free joined
                        form + the exact literal (pdb.qualified_name / pdb.literal)
    4. dense         -- cosine similarity over FROZEN vectors, read through the ONE
                        canonical frozen-embedding API (provenance.retrieve.embedder)

It also applies the two retention filters that make a record admissible, reports
gold_symbol_df, and FAILS if mean grep recall@10 on the retained set is >= 0.35.

THREE DEFECTS THIS FILE IS THE FIX FOR. Every one of them is a regression waiting to
happen, so each is named here and again at its site:

* (audit #12, the worst defect in the tree) This script used to carry its OWN private
  ``GoldenRecord`` reading ``query_id`` / ``anchors``. The committed golden set
  (`provenance/eval/golden/ci_subset.jsonl`) uses ``qid`` / ``required_evidence.
  symbol_anchors``. Run against the real file, every record parsed as empty -> zero
  gold -> recall 0.0 -> **verdict PASS**, a silent false-green on the one invariant
  that validates every other number in the project. There is now exactly ONE loader,
  `provenance.eval.schema.load_golden_set`, and a zero-gold run can no longer print
  PASS: it prints INCONCLUSIVE and exits non-zero (see `_verdict`).

* (audit #11) It called `provenance.eval.anchors.resolve_anchors(anchors, pool)`. The
  real signature is `(record, conn, *, repo_id, ...)`, and the try/except guarded only
  the *import* while the call sat in the `else`, so it raised AttributeError on every
  run and the "fallback" underneath was dead code. The DB path now calls the project
  resolver with its real signature; the fixture path is a separate, explicit
  implementation over an in-memory pool that speaks the same EvidenceKey vocabulary.

* (audit #16) It defined a fourth .npz format (four parallel arrays). Frozen vectors
  now come from `provenance.retrieve.embedder` -- one key function, one path template,
  one flat {embedding_key: vector} mapping shared with the eval runner.

ZERO network calls. ZERO LLM calls. ZERO embedding-API calls. There is no live
embedding path in this file to fall back to, by construction.

Usage
-----
    # against the live index (the normal case)
    python scripts/leakage_probe.py --from-db

    # against a committed fixture pool, no database
    python scripts/leakage_probe.py --pool <a-committed-pool>.jsonl \
        --json-out artifacts/leakage_probe.json

There is deliberately no default pool. `eval/fixtures/pool.jsonl` was the old default and
has never existed, so the script's own usage line pointed at a missing file; an implicit
empty pool would score every baseline 0.0 and read as a clean bill of health.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from provenance.config import settings
from provenance.eval import schema as golden_schema
from provenance.eval.schema import EvidenceKey, GoldenRecord, load_golden_set
from provenance.parse.chunker import split_identifier
from provenance.retrieve.embedder import FrozenEmbedder, FrozenEmbeddingMiss, fixture_path

# The threshold that defines "this benchmark measures something". Chosen, not tuned:
# grep@10 of 0.35 means roughly two thirds of retained queries cannot be answered by
# substring matching at all. Above that, lift over a neural system is unmeasurable.
# Same constant the schema retention rule uses; imported so they cannot drift apart.
GREP_RECALL_AT_10_CEILING = golden_schema.RETAINED_GREP_R10_MAX

# A token appearing in <= this many documents is "rare enough to be a giveaway".
# df 1 is a primary key; df 5 is still effectively a lookup on a corpus of 10^4+ docs.
RARE_DF_THRESHOLD = 5

DEFAULT_K = 10

#: The ONE committed golden set. Three different paths were referenced across CI, the
#: Makefile and this script's defaults (`eval/golden/golden.jsonl`, `golden_set.jsonl`);
#: only this one exists. Derived from the package, not typed as a repo-relative string,
#: so it resolves the same from any working directory.
DEFAULT_GOLDEN = Path(golden_schema.__file__).resolve().parent / "golden" / "ci_subset.jsonl"

# Question words carry no retrieval signal but do inflate the literal-extraction step.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "because",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "don't",
        "doesn't",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "should",
        "so",
        "than",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Document:
    """One retrievable unit in the pool.

    ``evidence`` is the fixture-mode stand-in for the ``chunk_evidence`` table: the
    stable keys (commit sha / PR number / issue number / review-comment id) this chunk
    is evidence for, in exactly the `EvidenceKey` spelling `GoldEvidence.keys()`
    produces. In `--from-db` mode it is empty and the real table is consulted instead.
    """

    chunk_id: str
    content: str
    path: str | None = None
    qualified_name: str | None = None
    evidence: frozenset[EvidenceKey] = frozenset()

    @property
    def symbol_key(self) -> EvidenceKey:
        """Same spelling as `provenance.eval.schema.SymbolAnchor.key`."""
        return ("symbol", f"{self.path or ''}::{self.qualified_name or ''}")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:  # a malformed pool must be loud
                raise SystemExit(f"{path}:{lineno}: malformed JSON: {exc}") from exc
    return rows


def _evidence_keys(raw: Any) -> frozenset[EvidenceKey]:
    """``[["pr", "47798"], ...]`` or ``{"pr": ["47798"], ...}`` -> EvidenceKey set."""
    keys: set[EvidenceKey] = set()
    if isinstance(raw, dict):
        for kind, values in raw.items():
            for value in values if isinstance(values, list) else [values]:
                keys.add((str(kind), str(value)))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, list | tuple) and len(item) == 2:
                keys.add((str(item[0]), str(item[1])))
    return frozenset(keys)


def load_pool(path: Path) -> list[Document]:
    docs = [
        Document(
            chunk_id=str(r["chunk_id"]),
            content=r.get("content") or "",
            path=r.get("path"),
            qualified_name=r.get("qualified_name"),
            evidence=_evidence_keys(r.get("evidence")),
        )
        for r in load_jsonl(path)
    ]
    if not docs:
        raise SystemExit(f"document pool {path} is empty -- nothing to probe")
    return docs


def load_pool_from_db(conn: Any, repo_id: int) -> list[Document]:
    """Pull the pool straight out of Postgres, for a run against the live index.

    Physical names come from `provenance.graph.tables`; none is typed here. The
    previous version selected `commit_sha, pr_number, issue_number, review_comment_id`
    FROM a table called `chunk` -- neither the table nor any of those columns exists
    (audit #2, #3). Commit/PR/issue evidence lives in `chunk_evidence` and is reached
    through the project resolver, not through columns on `chunks`.
    """
    from provenance.graph.tables import (
        CHUNKS,
        COL_CHUNK_ID,
        COL_REPO_ID,
        COL_TOMBSTONE,
    )

    sql = f"""
        SELECT {COL_CHUNK_ID}, content, path, qualified_name
        FROM {CHUNKS}
        WHERE {COL_REPO_ID} = %s
          AND {COL_TOMBSTONE} IS NULL
        ORDER BY {COL_CHUNK_ID}
    """
    # Rows are TUPLES: graph.db.connection defaults to tuple_row. Positional
    # unpacking is correct BY DEFAULT here and must not be "fixed" to dict access.
    with conn.cursor() as cur:
        cur.execute(sql, (repo_id,))
        return [
            Document(chunk_id=row[0], content=row[1] or "", path=row[2], qualified_name=row[3])
            for row in cur.fetchall()
        ]


# --------------------------------------------------------------------- anchor resolve


@dataclass
class ResolvedGold:
    """Required-tier gold for one record, as chunk ids in the current pool."""

    qid: str
    chunk_ids: set[str] = field(default_factory=set)
    unresolved: list[EvidenceKey] = field(default_factory=list)


def resolve_gold_from_pool(
    record: GoldenRecord, index: dict[EvidenceKey, set[str]]
) -> ResolvedGold:
    """Resolve one record's REQUIRED anchors against an in-memory pool index.

    This is the standalone (`--pool`) path. It is deliberately a separate function from
    the database path rather than a reimplementation of it: the database resolver joins
    `chunk_evidence`, which a JSONL fixture does not have. What the two share is the
    `EvidenceKey` vocabulary -- ("commit", sha) | ("pr", n) | ("issue", n) |
    ("review_comment", n) | ("symbol", "path::qualified_name") -- produced by
    `GoldEvidence.keys()`, so a record resolves to the same evidence in both modes.
    """
    resolved = ResolvedGold(qid=record.qid)
    for key in record.required_evidence:
        hits = index.get(key)
        if hits:
            resolved.chunk_ids |= hits
        else:
            resolved.unresolved.append(key)
    return resolved


def build_pool_index(pool: Sequence[Document]) -> dict[EvidenceKey, set[str]]:
    """EvidenceKey -> chunk ids, for the standalone pool path.

    File-level symbol anchors (``qualified_name=None``) are indexed under BOTH the
    file-level key and every chunk in that file, because a gold anchor naming only a
    path means "any chunk of this file", not "the chunk whose qualified_name is empty".
    """
    index: dict[EvidenceKey, set[str]] = {}
    for doc in pool:
        index.setdefault(doc.symbol_key, set()).add(doc.chunk_id)
        if doc.path:
            index.setdefault(("symbol", f"{doc.path}::"), set()).add(doc.chunk_id)
        for key in doc.evidence:
            index.setdefault(key, set()).add(doc.chunk_id)
    return index


def resolve_gold_from_db(record: GoldenRecord, conn: Any, repo_id: int) -> ResolvedGold:
    """Resolve through the project's own resolver, with its REAL signature.

    `resolve_anchors` is `(record: GoldenRecord, conn, *, repo_id: int, ...)`. The old
    call here was `resolve_anchors(anchors, pool)` -- wrong arity, wrong types -- and it
    sat in the `else` of a try/except that guarded only the import, so it raised
    AttributeError on every single run while looking guarded (audit #11). Import and
    call are both inside this function now, and neither is wrapped in a bare except:
    a resolver that cannot run is a broken gate, not a degraded one.
    """
    from provenance.eval.anchors import resolve_anchors

    resolved = resolve_anchors(record, conn, repo_id=repo_id)
    return ResolvedGold(
        qid=record.qid,
        chunk_ids=set(resolved.required_chunk_ids),
        unresolved=sorted(resolved.unresolved_required),
    )


# ---------------------------------------------------------------------- tokenizers

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize_default(text: str) -> list[str]:
    """The tokenizer everyone reaches for first. Keeps whole identifiers intact."""
    return [m.group(0).lower() for m in _WORD_RE.finditer(text)]


def tokenize_identifier_aware(text: str) -> list[str]:
    """Split parts + underscore-free joined form + the exact literal.

    This mirrors the index we actually build. `split_identifier` is IMPORTED from
    `provenance.parse.chunker` rather than re-implemented: it is the function that
    produces `chunks.lexical_blob`, and a second copy that drifts would turn the
    strongest cheap baseline into a strawman -- which is the same class of error as
    the one this whole script exists to catch. Verified ParadeDB behaviour:

        '_normalise_url camelCaseName'::pdb.source_code -> {normalise,url,camel,case,name}

    so the whole identifier does NOT survive pdb.source_code; the exact form is only
    searchable through the separate `qualified_name::pdb.literal` field, and the joined
    form only because the ingester emits it. Modelling all three is what makes
    bm25_ident an honest ceiling.
    """
    out: list[str] = []
    for m in _WORD_RE.finditer(text):
        raw = m.group(0)
        parts = split_identifier(raw)
        out.extend(parts)
        lowered = raw.lower()
        if len(parts) > 1:
            joined = "".join(parts)
            out.append(joined)  # underscore-free joined form
            if lowered != joined:
                out.append(lowered)  # exact literal form
    return out


def extract_literals(question: str) -> list[str]:
    """Candidate substrings a human running `grep` would actually type.

    Backticked and quoted spans first, then identifier-shaped words. Case is preserved:
    the whole point of the grep baseline is *literal* matching.
    """
    literals: list[str] = []
    for span in re.findall(r"`([^`]+)`", question) + re.findall(r'"([^"]+)"', question):
        cleaned = span.strip()
        if cleaned:
            literals.append(cleaned)
    for m in _WORD_RE.finditer(question):
        raw = m.group(0)
        if raw.lower() in _STOPWORDS or len(raw) < 4:
            continue
        looks_like_identifier = "_" in raw or any(c.isupper() for c in raw[1:]) or raw.islower()
        if looks_like_identifier:
            literals.append(raw)
    # dotted paths: airflow.models.dagrun, taskinstance.py
    literals.extend(re.findall(r"\b[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+\b", question))
    seen: set[str] = set()
    return [x for x in literals if not (x in seen or seen.add(x))]


# ------------------------------------------------------------------------ scorers


class BM25:
    """Plain Okapi BM25. Small, exact, no external dependency, no hidden defaults."""

    def __init__(self, docs: Sequence[Sequence[str]], k1: float = 1.2, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.n = len(docs)
        self.tf: list[Counter[str]] = [Counter(d) for d in docs]
        self.lengths = np.array([len(d) for d in docs], dtype=np.float64)
        self.avgdl = float(self.lengths.mean()) if self.n else 0.0
        self.postings: dict[str, list[int]] = {}
        for i, doc in enumerate(docs):
            for tok in set(doc):
                self.postings.setdefault(tok, []).append(i)
        self.df: dict[str, int] = {t: len(p) for t, p in self.postings.items()}

    def idf(self, token: str) -> float:
        df = self.df.get(token, 0)
        if df == 0:
            return 0.0
        return math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))

    def score(self, query_tokens: Sequence[str]) -> np.ndarray:
        scores = np.zeros(self.n, dtype=np.float64)
        if self.avgdl == 0.0:
            return scores
        for token in set(query_tokens):
            postings = self.postings.get(token)
            if not postings:
                continue
            idf = self.idf(token)
            for i in postings:
                f = self.tf[i][token]
                denom = f + self.k1 * (1.0 - self.b + self.b * self.lengths[i] / self.avgdl)
                scores[i] += idf * (f * (self.k1 + 1.0)) / denom
        return scores


class GrepIndex:
    """Pure literal substring search -- the baseline that must NOT win.

    Deliberately built as strongly as is reasonable: rarer literals count for more, and
    shorter documents break ties (a short doc containing the literal is a more precise
    hit). A weak grep baseline would let a broken benchmark through, which is the
    failure this file exists to prevent -- so the bias is towards making grep look good.
    """

    def __init__(self, docs: Sequence[Document]) -> None:
        self.contents = [d.content for d in docs]
        self.lengths = np.array([max(len(c), 1) for c in self.contents], dtype=np.float64)
        self._df_cache: dict[str, list[int]] = {}

    def postings(self, literal: str) -> list[int]:
        cached = self._df_cache.get(literal)
        if cached is None:
            cached = [i for i, c in enumerate(self.contents) if literal in c]
            self._df_cache[literal] = cached
        return cached

    def substring_df(self, literal: str) -> int:
        return len(self.postings(literal))

    def score(self, question: str) -> np.ndarray:
        scores = np.zeros(len(self.contents), dtype=np.float64)
        for literal in extract_literals(question):
            postings = self.postings(literal)
            if not postings:
                continue
            weight = 1.0 / math.log2(2.0 + len(postings))
            for i in postings:
                scores[i] += weight
        # tie-break: shorter document wins, as a strict sub-epsilon nudge
        nonzero = scores > 0
        scores[nonzero] += 1e-6 / np.log2(2.0 + self.lengths[nonzero])
        return scores


class DenseBaseline:
    """Cosine similarity over frozen vectors from the ONE canonical cache.

    Everything about the .npz -- its path, its key function, its flat
    {embedding_key(model_id, text): float32[dim]} layout -- belongs to
    `provenance.retrieve.embedder`. This class holds no format knowledge of its own;
    it just calls `encode_documents` / `encode_query`. The three incompatible caches
    (audit #16) are why the eval gate and this probe could never share a file.

    MONEY SAFETY: `FrozenEmbedder` raises on a miss and has no live fallback branch.
    Nothing in this class adds one. A missing vector makes the dense ROW unmeasurable,
    never a network call.
    """

    def __init__(self, embedder: FrozenEmbedder, pool: Sequence[Document]) -> None:
        self.embedder = embedder
        # Pre-flight every document. A partially-covered pool cannot be scored: docs
        # with no vector would silently rank last, understating dense and overstating
        # the lift of anything compared against it.
        missing = [d.chunk_id for d in pool if not embedder.has(d.content)]
        if missing:
            raise FrozenEmbeddingMiss(
                f"{len(missing)}/{len(pool)} pooled documents have no frozen vector in "
                f"{embedder.path} (first: {missing[0]}). Re-bake the cache offline; "
                "this probe will not make a live embedding call to fill the gap."
            )
        self.matrix = embedder.encode_documents([d.content for d in pool])

    def score(self, question: str) -> np.ndarray:
        # A query miss IS fatal, unlike a document miss: the eval runner pre-flights
        # every question against this same cache, so a missing query vector means the
        # bake step is broken rather than incomplete.
        return self.matrix @ self.embedder.encode_query(question)


def rank(scores: np.ndarray, k: int) -> list[int]:
    """Indices of the top-k documents, descending. Ties break on lower index (stable)."""
    k = min(k, scores.shape[0])
    if k == 0:
        return []
    top = np.argpartition(-scores, k - 1)[:k]
    return [int(i) for i in top[np.argsort(-scores[top], kind="stable")]]


# --------------------------------------------------------------- retention filters


def grep_answers_at_rank_1(
    question: str,
    grep: GrepIndex,
    pool: Sequence[Document],
    gold_chunk_ids: set[str],
) -> bool:
    """RETENTION FILTER 1: discard any query that literal grep answers at rank 1.

    If `grep` puts a gold document first, the query is a lookup, not a question about
    rationale. Keeping it inflates every system's score by the same amount and destroys
    discrimination between systems.
    """
    order = rank(grep.score(question), 1)
    if not order:
        return False
    return pool[order[0]].chunk_id in gold_chunk_ids


def rare_tokens_shared_with_gold(
    question: str,
    gold_documents: Sequence[Document],
    corpus_df: dict[str, int],
    max_df: int = RARE_DF_THRESHOLD,
) -> list[str]:
    """Tokens of df <= max_df present in BOTH the question and a gold document.

    RETENTION FILTER 2. A question sharing a df<=5 token with its own gold document has
    the answer's primary key printed on its face. This is the single defect that
    produced grep recall@10 = 0.986 on the first Airflow golden set.
    """
    q_tokens = set(tokenize_identifier_aware(question))
    gold_tokens: set[str] = set()
    for doc in gold_documents:
        gold_tokens.update(tokenize_identifier_aware(doc.content))
    shared = q_tokens & gold_tokens
    return sorted(t for t in shared if corpus_df.get(t, 0) <= max_df)


def gold_symbol_literals(record: GoldenRecord) -> list[str]:
    """The literal strings a grep user would type for this record's gold symbols.

    A qualified name like ``SchedulerJobRunner._executable_task_instances_to_queued``
    never appears verbatim in source; its segments do. df is therefore measured on the
    RAREST segment, which is the one that makes the query a lookup.
    """
    literals: list[str] = []
    for anchor in record.required_evidence.symbol_anchors:
        if anchor.qualified_name:
            literals.extend(part for part in anchor.qualified_name.split(".") if part)
        elif anchor.path:
            literals.append(Path(anchor.path).name)
    return literals


# ------------------------------------------------------------------------ metrics


@dataclass
class BaselineResult:
    name: str
    recall_at_1: float = 0.0
    recall_at_10: float = 0.0
    mrr_at_10: float = 0.0
    measured: bool = True
    note: str = ""


def evaluate(
    name: str,
    rankings: dict[str, list[str]],
    gold: dict[str, set[str]],
    k: int,
) -> BaselineResult:
    """Hit-rate style recall@k over the required tier: did ANY gold chunk make the cut.

    Deliberately the same definition for all four rows. It is not the runner's
    fraction-of-required-evidence recall; the point here is comparability BETWEEN
    baselines, and a hit-rate is the most generous reading for grep.
    """
    r1: list[int] = []
    r10: list[int] = []
    rr: list[float] = []
    for qid, ranked in rankings.items():
        g = gold[qid]
        r1.append(1 if ranked[:1] and ranked[0] in g else 0)
        r10.append(1 if any(c in g for c in ranked[:k]) else 0)
        recip = 0.0
        for pos, chunk in enumerate(ranked[:k], 1):
            if chunk in g:
                recip = 1.0 / pos
                break
        rr.append(recip)
    n = max(len(r1), 1)
    return BaselineResult(
        name=name,
        recall_at_1=sum(r1) / n,
        recall_at_10=sum(r10) / n,
        mrr_at_10=sum(rr) / n,
    )


# --------------------------------------------------------------------------- probe

#: Verdicts. INCONCLUSIVE exists because of audit #12: a run with zero resolved gold
#: used to compute recall 0.0 and print PASS. Zero gold does not mean "grep cannot
#: answer this benchmark", it means THERE WAS NO BENCHMARK, and the two must never
#: again render as the same word.
PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"


def _verdict(n_retained: int, grep_recall_at_10: float) -> str:
    if n_retained == 0:
        return INCONCLUSIVE
    return FAIL if grep_recall_at_10 >= GREP_RECALL_AT_10_CEILING else PASS


def run_probe(
    golden: Sequence[GoldenRecord],
    pool: Sequence[Document],
    resolver: Callable[[GoldenRecord], ResolvedGold],
    *,
    dense: DenseBaseline | None = None,
    dense_note: str = "",
    k: int = DEFAULT_K,
) -> dict[str, Any]:
    grep = GrepIndex(pool)
    bm25_default = BM25([tokenize_default(d.content) for d in pool])
    bm25_ident = BM25([tokenize_identifier_aware(d.content) for d in pool])

    corpus_df = dict(bm25_ident.df)
    chunk_ids = [d.chunk_id for d in pool]
    by_chunk = {d.chunk_id: d for d in pool}

    retained: list[GoldenRecord] = []
    dropped: list[dict[str, str]] = []
    gold_map: dict[str, set[str]] = {}
    gold_symbol_dfs: list[int] = []
    unresolved_keys: dict[str, list[str]] = {}

    for rec in golden:
        # Unanswerables carry no required evidence BY CONSTRUCTION (schema validator).
        # They are scored by refusal precision/recall, not by retrieval, and counting
        # them as "anchors resolved to nothing" would hide real resolution failures in
        # a pile of expected ones.
        if rec.expected_refusal:
            dropped.append({"qid": rec.qid, "reason": "unanswerable_scored_by_refusal"})
            continue

        resolved = resolver(rec)
        if resolved.unresolved:
            unresolved_keys[rec.qid] = [f"{kind}:{value}" for kind, value in resolved.unresolved]
        if not resolved.chunk_ids:
            dropped.append({"qid": rec.qid, "reason": "anchors_resolve_to_nothing"})
            continue
        gold_map[rec.qid] = resolved.chunk_ids

        for literal in gold_symbol_literals(rec):
            gold_symbol_dfs.append(grep.substring_df(literal))

        if grep_answers_at_rank_1(rec.question, grep, pool, resolved.chunk_ids):
            dropped.append({"qid": rec.qid, "reason": "grep_answers_at_rank_1"})
            continue

        gold_docs = [by_chunk[c] for c in resolved.chunk_ids if c in by_chunk]
        shared = rare_tokens_shared_with_gold(rec.question, gold_docs, corpus_df)
        if shared:
            dropped.append(
                {"qid": rec.qid, "reason": f"rare_token_overlap: {', '.join(shared[:5])}"}
            )
            continue

        retained.append(rec)

    def rankings_for(scorer: Callable[[GoldenRecord], np.ndarray]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for rec in retained:
            out[rec.qid] = [chunk_ids[i] for i in rank(scorer(rec), k)]
        return out

    results: list[BaselineResult] = [
        evaluate("grep", rankings_for(lambda r: grep.score(r.question)), gold_map, k),
        evaluate(
            "bm25_default",
            rankings_for(lambda r: bm25_default.score(tokenize_default(r.question))),
            gold_map,
            k,
        ),
        evaluate(
            "bm25_ident",
            rankings_for(lambda r: bm25_ident.score(tokenize_identifier_aware(r.question))),
            gold_map,
            k,
        ),
    ]
    if dense is not None:
        dense_rankings = rankings_for(lambda r: dense.score(r.question))
        results.append(evaluate("dense", dense_rankings, gold_map, k))
    else:
        results.append(
            BaselineResult(name="dense", measured=False, note=dense_note or "no frozen vectors")
        )

    grep_result = results[0]
    cheap = [r for r in results if r.name != "dense" and r.measured]
    strongest_cheap = max(cheap, key=lambda r: r.recall_at_10)
    verdict = _verdict(len(retained), grep_result.recall_at_10)

    return {
        "k": k,
        "n_golden": len(golden),
        "n_retained": len(retained),
        "n_dropped": len(dropped),
        "retention_rate": len(retained) / max(len(golden), 1),
        "pool_size": len(pool),
        "gold_symbol_df": {
            "n": len(gold_symbol_dfs),
            "median": statistics.median(gold_symbol_dfs) if gold_symbol_dfs else None,
            "mean": statistics.fmean(gold_symbol_dfs) if gold_symbol_dfs else None,
            "min": min(gold_symbol_dfs) if gold_symbol_dfs else None,
            "max": max(gold_symbol_dfs) if gold_symbol_dfs else None,
            "frac_df_le_1": (
                sum(1 for d in gold_symbol_dfs if d <= 1) / len(gold_symbol_dfs)
                if gold_symbol_dfs
                else None
            ),
        },
        "baselines": [
            {
                "name": r.name,
                "measured": r.measured,
                "note": r.note,
                "recall_at_1": r.recall_at_1,
                "recall_at_10": r.recall_at_10,
                "mrr_at_10": r.mrr_at_10,
            }
            for r in results
        ],
        "strongest_cheap_baseline": strongest_cheap.name,
        "strongest_cheap_recall_at_10": strongest_cheap.recall_at_10,
        "grep_recall_at_10": grep_result.recall_at_10,
        "ceiling": GREP_RECALL_AT_10_CEILING,
        "verdict": verdict,
        "unresolved_anchors": unresolved_keys,
        "dropped": dropped[:200],
        "drop_reason_counts": dict(Counter(d["reason"].split(":", 1)[0] for d in dropped)),
    }


# -------------------------------------------------------------------------- report


def render_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 78)
    add("PACE LEAKAGE PROBE -- can substring search ace this benchmark?")
    add("=" * 78)
    add("")
    add(f"  document pool          {report['pool_size']:>8,} chunks")
    add(f"  golden records loaded  {report['n_golden']:>8,}")
    add(f"  retained after filters {report['n_retained']:>8,}   ({report['retention_rate']:.1%})")
    add(f"  dropped                {report['n_dropped']:>8,}")
    for reason, count in sorted(report["drop_reason_counts"].items(), key=lambda x: -x[1]):
        add(f"      - {reason:<38} {count:>6,}")
    if report["unresolved_anchors"]:
        add("")
        add(f"  UNRESOLVED ANCHORS on {len(report['unresolved_anchors'])} record(s) -- an ingest")
        add("  gap or a scoring bug, NEVER a retrieval miss:")
        for qid, keys in list(report["unresolved_anchors"].items())[:10]:
            add(f"      {qid}: {', '.join(keys[:6])}")
    add("")

    gsd = report["gold_symbol_df"]
    add("  gold_symbol document frequency (how unique is the answer's primary key?)")
    if gsd["n"]:
        add(
            f"      median {gsd['median']}   mean {gsd['mean']:.2f}   "
            f"min {gsd['min']}   max {gsd['max']}   (n={gsd['n']} symbols)"
        )
        add(
            f"      fraction with df <= 1: {gsd['frac_df_le_1']:.1%}"
            "   (df==1 means the symbol IS the answer)"
        )
    else:
        add("      no resolvable gold symbol on any record")
    add("")

    add("  BASELINES (lift must be measured over the STRONGEST row, not the weakest)")
    add(f"      {'baseline':<16}{'recall@1':>10}{'recall@10':>12}{'MRR@10':>10}")
    add(f"      {'-' * 48}")
    for b in report["baselines"]:
        if not b["measured"]:
            add(f"      {b['name']:<16}{'NOT MEASURED':>32}   {b['note']}")
            continue
        marker = "  <-- strongest cheap" if b["name"] == report["strongest_cheap_baseline"] else ""
        add(
            f"      {b['name']:<16}{b['recall_at_1']:>10.3f}"
            f"{b['recall_at_10']:>12.3f}{b['mrr_at_10']:>10.3f}{marker}"
        )
    add("")
    add(f"  grep recall@10 = {report['grep_recall_at_10']:.3f}   ceiling = {report['ceiling']:.2f}")
    add("")

    verdict = report["verdict"]
    if verdict == PASS:
        add("  VERDICT: PASS")
        add("      Literal substring search cannot answer the retained set. Retrieval")
        add("      numbers measured on it are meaningful, and lift must be quoted")
        add(
            f"      against '{report['strongest_cheap_baseline']}' "
            f"(recall@10 {report['strongest_cheap_recall_at_10']:.3f})."
        )
    elif verdict == FAIL:
        add("  VERDICT: FAIL  ***  THE BENCHMARK MEASURES NOTHING  ***")
        add("      grep answers the retained set. Every downstream number -- ablations,")
        add("      reranker lift, refusal precision -- is invalid until this is fixed.")
        add("      Fix by rewriting questions so they describe the RATIONALE sought")
        add("      rather than naming the symbol, and re-running the retention filters.")
    else:
        add("  VERDICT: INCONCLUSIVE  ***  NOT A PASS  ***")
        add("      Zero records survived to be scored, so nothing was measured. This is")
        add("      the expected state before the first ingest and the expected state")
        add("      when the golden set is empty -- but it is NOT evidence that the")
        add("      benchmark is sound. A run that resolved no gold once printed PASS")
        add("      (recall 0.0 < 0.35); that false green is what this verdict replaces.")
    add("")
    add(_meeting_summary(report))
    add("=" * 78)
    return "\n".join(lines)


def _meeting_summary(report: dict[str, Any]) -> str:
    """One paragraph, pasteable into a supervisor meeting or a progress report."""
    gsd = report["gold_symbol_df"]
    df_clause = (
        f"median gold-symbol document frequency {gsd['median']} "
        f"({gsd['frac_df_le_1']:.0%} of gold symbols appear in <= 1 document)"
        if gsd["n"]
        else "no gold symbol was resolvable, so document frequency is unmeasured"
    )
    if report["verdict"] == INCONCLUSIVE:
        body = (
            f"Nothing was scored: {report['n_golden']} golden record(s) were loaded against a "
            f"pool of {report['pool_size']:,} chunks and none survived anchor resolution and "
            "the retention filters. No claim about benchmark quality can be made from this "
            "run, and no retrieval number measured today should be quoted."
        )
    else:
        rows = {b["name"]: b for b in report["baselines"]}
        cheap_bits = ", ".join(
            f"{name} {rows[name]['recall_at_10']:.3f}"
            for name in ("grep", "bm25_default", "bm25_ident")
            if rows[name]["measured"]
        )
        dense_bit = (
            f"dense-only {rows['dense']['recall_at_10']:.3f}"
            if rows["dense"]["measured"]
            else "dense-only not measured (no frozen vectors committed yet)"
        )
        body = (
            f"On {report['n_retained']} retained queries "
            f"({report['retention_rate']:.0%} of {report['n_golden']}) over a pool of "
            f"{report['pool_size']:,} chunks, cheap-baseline recall@{report['k']} is "
            f"{cheap_bits}; {dense_bit}. The strongest cheap baseline is "
            f"'{report['strongest_cheap_baseline']}' at "
            f"{report['strongest_cheap_recall_at_10']:.3f}, and all reported lift is quoted "
            f"against it. {df_clause.capitalize()}. Substring search scores "
            f"{report['grep_recall_at_10']:.3f} against a ceiling of {report['ceiling']:.2f}, so "
            f"the verdict is {report['verdict']}."
        )
    wrapped: list[str] = ["  SUMMARY (pasteable)", "  " + "-" * 48]
    line = "  "
    for word in body.split():
        if len(line) + len(word) + 1 > 78:
            wrapped.append(line)
            line = "  "
        line += ("" if line.strip() == "" else " ") + word
    wrapped.append(line)
    return "\n".join(wrapped)


# ----------------------------------------------------------------------------- cli


def _build_dense(
    pool: Sequence[Document], path: Path | None, model_id: str, *, require: bool
) -> tuple[DenseBaseline | None, str]:
    """Build the dense row, or explain in one line why it is unmeasurable.

    Not measuring dense is a reporting gap; it is never a licence to embed live. The
    only two outcomes here are "measured from the committed .npz" and "not measured".
    """
    try:
        embedder = FrozenEmbedder(path, model_id=model_id)
        return DenseBaseline(embedder, pool), ""
    except FrozenEmbeddingMiss as exc:
        note = str(exc).splitlines()[0]
        if require:
            raise SystemExit(f"--require-dense: {exc}") from exc
        return None, note


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--golden",
        type=Path,
        default=DEFAULT_GOLDEN,
        help="Golden set .jsonl (default: the committed CI subset).",
    )
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--pool", type=Path, help="Committed document pool .jsonl.")
    source.add_argument(
        "--from-db",
        action="store_true",
        help="Read the pool from the live index.",
    )
    p.add_argument(
        "--repo",
        default=f"{settings.repo_owner}/{settings.repo_name}",
        help="owner/name of the corpus; resolved to repositories.repo_id (--from-db).",
    )
    p.add_argument(
        "--embeddings",
        type=Path,
        default=None,
        help="Frozen .npz; defaults to the canonical eval/fixtures/qvecs_<model>.npz.",
    )
    p.add_argument("--model", default=settings.embedding_model_version, help="Embedding model id.")
    p.add_argument("--no-dense", action="store_true", help="Skip the dense row entirely.")
    p.add_argument(
        "--require-dense",
        action="store_true",
        help="Fail (exit 2) if the dense row cannot be measured from frozen vectors.",
    )
    p.add_argument(
        "--allow-inconclusive",
        action="store_true",
        help="Exit 0 when nothing could be scored (pre-ingest only; still prints NOT A PASS).",
    )
    p.add_argument(
        "--k",
        type=int,
        default=DEFAULT_K,
        help=(
            "Retrieval cut-off. The report's `recall_at_10` column is recall at THIS k; "
            "the 0.35 ceiling was chosen at k=10, so moving it moves the gate."
        ),
    )
    p.add_argument("--json-out", type=Path, default=None)
    args = p.parse_args(argv)

    golden = load_golden_set(args.golden)

    if args.from_db:
        from provenance.graph.db import connection
        from provenance.graph.repos import UnknownRepository, resolve_repo_id, split_repo_slug

        owner, name = split_repo_slug(args.repo)
        with connection() as conn:
            repo_id: int | None = None
            try:
                repo_id = resolve_repo_id(conn, owner, name)
            except UnknownRepository as exc:
                # Not ingested yet. This falls THROUGH to the normal reporting path
                # with an empty pool rather than returning early, so the run still
                # renders a verdict and still writes --json-out. An early return left
                # no JSON behind, and the CI step that reads the verdict out of that
                # file would then die on a missing path -- turning "nothing is
                # ingested" into a red build for the wrong reason.
                print(f"leakage probe: {exc}", file=sys.stderr)

            pool = load_pool_from_db(conn, repo_id) if repo_id is not None else []

            def db_resolver(rec: GoldenRecord) -> ResolvedGold:
                if repo_id is None:
                    return ResolvedGold(qid=rec.qid)
                return resolve_gold_from_db(rec, conn, repo_id)

            # Both the pool read and the anchor resolution need the connection open,
            # so the whole report is built inside the `with`.
            dense, note = (
                (None, "disabled with --no-dense")
                if args.no_dense
                else _build_dense(pool, args.embeddings, args.model, require=args.require_dense)
            )
            report = run_probe(golden, pool, db_resolver, dense=dense, dense_note=note, k=args.k)
    else:
        pool = load_pool(args.pool)
        index = build_pool_index(pool)

        def pool_resolver(rec: GoldenRecord) -> ResolvedGold:
            return resolve_gold_from_pool(rec, index)

        dense, note = (
            (None, "disabled with --no-dense")
            if args.no_dense
            else _build_dense(pool, args.embeddings, args.model, require=args.require_dense)
        )
        report = run_probe(golden, pool, pool_resolver, dense=dense, dense_note=note, k=args.k)

    report["golden_path"] = str(args.golden)
    report["frozen_embeddings_path"] = str(args.embeddings or fixture_path(args.model))
    print(render_report(report))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nwrote {args.json_out}")

    if report["verdict"] == PASS:
        return 0
    if report["verdict"] == FAIL:
        return 1
    return 0 if args.allow_inconclusive else 2


if __name__ == "__main__":
    sys.exit(main())

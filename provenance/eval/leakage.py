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

It also applies the two retention filters that make a record admissible (ADR-0003 §5),
reports gold_symbol_df, and gives checkpoint B's verdict on the dev split (ADR-0003 §6):
INCONCLUSIVE below 100 retained records or when a stored fact is stale; FAIL when
retention is below 25% or the strongest lexical baseline's Wilson 95% upper bound on
recall@10 reaches 0.35; PASS otherwise.

THREE DEFECTS THIS FILE IS THE FIX FOR. Every one of them is a regression waiting to
happen, so each is named here and again at its site:

* (audit #12, the worst defect in the tree) This script used to carry its OWN private
  ``GoldenRecord`` reading ``query_id`` / ``anchors``. The committed golden set
  (`provenance/eval/golden/ci_subset.jsonl`) uses ``qid`` / ``required_evidence.
  symbol_anchors``. Run against the real file, every record parsed as empty -> zero
  gold -> recall 0.0 -> **verdict PASS**, a silent false-green on the one invariant
  that validates every other number in the project. There is now exactly ONE loader,
  `provenance.eval.schema.load_golden_set`, and a zero-gold run can no longer print
  PASS: it prints INCONCLUSIVE and exits non-zero (see `verdict`).

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
    pace leakage --from-db --golden provenance/eval/golden/auto_full.jsonl

    # against a committed fixture pool, no database
    pace leakage --pool <a-committed-pool>.jsonl --json-out artifacts/leakage_probe.json

`scripts/leakage_probe.py` runs this same `main`. It stays because CI and the nightly job
invoke the probe by that path.

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
from provenance.eval.metrics import wilson_interval
from provenance.eval.schema import EvidenceKey, GoldenRecord, load_golden_set
from provenance.parse.chunker import split_identifier
from provenance.retrieve.embedder import FrozenEmbedder, FrozenEmbeddingMiss, fixture_path

# The threshold that defines "this benchmark measures something" (ADR-0003 §6): the
# strongest lexical baseline's Wilson 95% upper bound on recall@10 must stay below it.
# Chosen, not tuned: at 0.35 roughly two thirds of retained queries are beyond every
# cheap lexical system, and above it lift over a neural system is unmeasurable.
# Defined once, in the schema; imported so the two cannot drift apart.
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

    Rationale text only (ADR-0003 §1): rows whose kind is in RATIONALE_KINDS. Without
    that filter, checkpoint C's code chunks would silently join the benchmark's pool.
    """
    from provenance.graph.tables import (
        CHUNKS,
        COL_CHUNK_ID,
        COL_REPO_ID,
        COL_TOMBSTONE,
        RATIONALE_KINDS,
    )

    sql = f"""
        SELECT {COL_CHUNK_ID}, content, path, qualified_name
        FROM {CHUNKS}
        WHERE {COL_REPO_ID} = %s
          AND {COL_TOMBSTONE} IS NULL
          AND kind = ANY(%s::artifact_kind[])
        ORDER BY {COL_CHUNK_ID}
    """
    # Rows are TUPLES: graph.db.connection defaults to tuple_row. Positional
    # unpacking is correct BY DEFAULT here and must not be "fixed" to dict access.
    with conn.cursor() as cur:
        cur.execute(sql, (repo_id, list(RATIONALE_KINDS)))
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
    # .keys() is required, not redundant: `required_evidence` is a pydantic model, not a
    # dict, and its default __iter__ yields (field_name, field_value) pairs -- iterating
    # it directly here once raised TypeError: unhashable type: 'list'.
    for key in record.required_evidence.keys():  # noqa: SIM118
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
    return list(dict.fromkeys(literals))  # first occurrence wins, order kept


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
        scores: np.ndarray = self.matrix @ self.embedder.encode_query(question)
        return scores


def rank(scores: np.ndarray, k: int) -> list[int]:
    """Indices of the top-k documents, descending. Ties break on lower index (stable).

    A full stable sort, not `argpartition`: partitioning keeps no order among tied
    documents, so `rank([0, 0, 1, 1], 1)` returned `[3]` and rank 1 could depend on k.
    At pool sizes in the thousands the sort costs well under a millisecond.
    """
    return [int(i) for i in np.argsort(-scores, kind="stable")[:k]]


# --------------------------------------------------------------- retention filters


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


# --------------------------------------------------------------- one record's facts


@dataclass(frozen=True)
class LeakageFacts:
    """One question's leakage against one pool (ADR-0003 §5).

    Generation stores these on the record (`schema.LeakageProbe`); the verdict measures
    them again with this same code and refuses to pass when the two disagree.
    """

    grep_answers_at_rank_1: bool
    rare_tokens_shared_with_gold: list[str]
    grep_hit_at_k: bool
    bm25_hit_at_k: bool
    gold_symbol_df: int | None

    def retained(self) -> bool:
        return not self.grep_answers_at_rank_1 and not self.rare_tokens_shared_with_gold

    def differs_from(self, stored: golden_schema.LeakageProbe) -> bool:
        """True when the retention facts stored at generation are not these."""
        return (stored.grep_answers_at_rank_1, stored.rare_tokens_shared_with_gold) != (
            self.grep_answers_at_rank_1,
            self.rare_tokens_shared_with_gold,
        )


class PoolIndex:
    """The probe's three lexical scorers over one pool, built once for every question."""

    def __init__(self, pool: Sequence[Document]) -> None:
        self.pool = list(pool)
        self.chunk_ids = [d.chunk_id for d in self.pool]
        self.by_chunk = {d.chunk_id: d for d in self.pool}
        self.grep = GrepIndex(self.pool)
        self.bm25_default = BM25([tokenize_default(d.content) for d in self.pool])
        self.bm25_ident = BM25([tokenize_identifier_aware(d.content) for d in self.pool])
        self.corpus_df = dict(self.bm25_ident.df)

    def matched(self, scores: np.ndarray, k: int) -> list[str]:
        """Chunk ids of the top-k documents that matched at all, best first.

        `rank` orders every document, so with fewer than k matches it fills the top k
        with documents that share nothing with the query, and with no match at all its
        first index would read as "rank 1". A lexical system returns only what matched:
        grep prints matching lines, a BM25 index returns matching rows.
        """
        return [self.chunk_ids[i] for i in rank(scores, k) if scores[i] > 0]

    def measure(
        self, question: str, gold: set[str], symbol: str | None = None, *, k: int = DEFAULT_K
    ) -> LeakageFacts:
        """`question`'s leakage against this pool, given its gold chunk ids.

        RETENTION FILTER 1 is grep ranking a gold document first: the query is then a
        lookup, not a question about rationale. RETENTION FILTER 2 is a token of pool
        df <= 5 shared with a gold document: the answer's primary key is printed on the
        question's face. `symbol` is the short name whose document frequency is reported.
        """
        grep_top = self.matched(self.grep.score(question), k)
        bm25_top = self.matched(self.bm25_ident.score(tokenize_identifier_aware(question)), k)
        gold_documents = [self.by_chunk[c] for c in sorted(gold) if c in self.by_chunk]
        return LeakageFacts(
            grep_answers_at_rank_1=bool(grep_top) and grep_top[0] in gold,
            rare_tokens_shared_with_gold=rare_tokens_shared_with_gold(
                question, gold_documents, self.corpus_df
            ),
            grep_hit_at_k=any(c in gold for c in grep_top),
            bm25_hit_at_k=any(c in gold for c in bm25_top),
            gold_symbol_df=None if symbol is None else self.grep.substring_df(symbol),
        )


# ------------------------------------------------------------------------ metrics


@dataclass
class BaselineResult:
    name: str
    recall_at_1: float = 0.0
    recall_at_10: float = 0.0
    mrr_at_10: float = 0.0
    measured: bool = True
    note: str = ""
    #: The numbers behind recall_at_10, for its Wilson interval: hits in the top k, of n.
    hits_at_k: int = 0
    n: int = 0


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
    n = len(r10)
    return BaselineResult(
        name=name,
        recall_at_1=sum(r1) / max(n, 1),
        recall_at_10=sum(r10) / max(n, 1),
        mrr_at_10=sum(rr) / max(n, 1),
        hits_at_k=sum(r10),
        n=n,
    )


# --------------------------------------------------------------------------- probe

#: Verdicts. INCONCLUSIVE exists because of audit #12: a run with zero resolved gold
#: used to compute recall 0.0 and print PASS. Too little measured, or a measurement of
#: the wrong pool, is not "grep cannot answer this benchmark", and the two must never
#: again render as the same word.
PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"

#: ADR-0003 §6, fixed on 2026-10-27, before any generated record existed. The gate reads
#: dev only; test is opened at most twice in the project (§7).
GATE_SPLIT = "dev"
MIN_RETAINED_DEV = 100
MIN_RETENTION = 0.25
#: The rows the gate reads. Dense is reported beside them and never enters the verdict:
#: a strong learned retriever is a result, not a leak.
LEXICAL_BASELINES = ("grep", "bm25_default", "bm25_ident")


@dataclass(frozen=True)
class Gate:
    """The numbers ADR-0003 §6's verdict reads, and nothing else."""

    n_candidates: int  # answerable dev records, before the filters
    n_retained: int
    stale: int  # records whose stored leakage facts differ from the current pool
    strongest: str  # the lexical baseline with the most hits in the top k
    strongest_upper: float  # its Wilson 95% upper bound on recall@k

    @property
    def retention(self) -> float:
        return self.n_retained / self.n_candidates if self.n_candidates else 0.0


def verdict(gate: Gate) -> tuple[str, list[str]]:
    """ADR-0003 §6: the verdict, and the reasons for anything but PASS.

    INCONCLUSIVE is decided first. A stale set was measured against a pool its records
    were not generated against, and fewer than 100 retained records cannot support a
    bound tight enough to mean anything: neither is evidence either way, and neither
    is a pass.
    """
    reasons: list[str] = []
    if gate.stale:
        reasons.append(
            f"{gate.stale:,} record(s) carry leakage facts that differ from this pool; "
            "regenerate with `pace golden generate`"
        )
    if gate.n_retained < MIN_RETAINED_DEV:
        reasons.append(
            f"{gate.n_retained:,} retained {GATE_SPLIT} records, fewer than {MIN_RETAINED_DEV}"
        )
    if reasons:
        return INCONCLUSIVE, reasons
    if gate.retention < MIN_RETENTION:
        reasons.append(f"retention {gate.retention:.1%} is below {MIN_RETENTION:.0%}")
    if gate.strongest_upper >= GREP_RECALL_AT_10_CEILING:
        reasons.append(
            f"{gate.strongest}'s Wilson 95% upper bound on recall@10, "
            f"{gate.strongest_upper:.3f}, is not below {GREP_RECALL_AT_10_CEILING:.2f}"
        )
    return (FAIL, reasons) if reasons else (PASS, [])


def symbol_short_name(record: GoldenRecord) -> str | None:
    """The short name of the record's symbol anchor (`grow` for `B.grow`), or None.

    A generated record carries its symbol as supporting evidence (ADR-0003 §4); a
    hand-written one may carry it as required evidence, which is read first.
    """
    for anchor in (
        *record.required_evidence.symbol_anchors,
        *record.supporting_evidence.symbol_anchors,
    ):
        if anchor.qualified_name:
            return anchor.qualified_name.rsplit(".", 1)[-1]
    return None


def run_probe(
    golden: Sequence[GoldenRecord],
    pool: Sequence[Document],
    resolver: Callable[[GoldenRecord], ResolvedGold],
    *,
    dense: DenseBaseline | None = None,
    dense_note: str = "",
    k: int = DEFAULT_K,
) -> dict[str, Any]:
    """Score the dev split's answerable records and give ADR-0003 §6's verdict.

    Every record's leakage facts are measured again with `PoolIndex.measure`, the code
    generation used; a generated record whose stored facts differ is stale. Retention
    is decided by the facts measured now, so the filter and the verdict cannot disagree
    about which records were scored.
    """
    index = PoolIndex(pool)
    in_split = [rec for rec in golden if rec.split == GATE_SPLIT]
    # Unanswerables carry no required evidence BY CONSTRUCTION (schema validator). They
    # are scored by refusal precision/recall, not by retrieval, so they are counted and
    # never become candidates.
    candidates = [rec for rec in in_split if not rec.expected_refusal]

    scored: list[GoldenRecord] = []
    retained: list[GoldenRecord] = []
    dropped: list[dict[str, str]] = []
    gold_map: dict[str, set[str]] = {}
    symbol_df: dict[str, int] = {}
    stale: list[str] = []
    unresolved_keys: dict[str, list[str]] = {}

    for rec in candidates:
        resolved = resolver(rec)
        if resolved.unresolved:
            unresolved_keys[rec.qid] = [f"{kind}:{value}" for kind, value in resolved.unresolved]
        if not resolved.chunk_ids:
            dropped.append({"qid": rec.qid, "reason": "anchors_resolve_to_nothing"})
            continue
        facts = index.measure(rec.question, resolved.chunk_ids, symbol_short_name(rec), k=k)
        if rec.leakage is not None and not rec.is_handwritten and facts.differs_from(rec.leakage):
            stale.append(rec.qid)
        gold_map[rec.qid] = resolved.chunk_ids
        if facts.gold_symbol_df is not None:
            symbol_df[rec.qid] = facts.gold_symbol_df
        scored.append(rec)
        if facts.grep_answers_at_rank_1:
            dropped.append({"qid": rec.qid, "reason": "grep_answers_at_rank_1"})
        elif facts.rare_tokens_shared_with_gold:
            shared = ", ".join(facts.rare_tokens_shared_with_gold[:5])
            dropped.append({"qid": rec.qid, "reason": f"rare_token_overlap: {shared}"})
        else:
            retained.append(rec)

    rankings = _rankings(index, scored, dense, k)
    retained_ids = [rec.qid for rec in retained]
    filtered = _baselines(rankings, gold_map, retained_ids, k, dense_note)
    unfiltered = _baselines(rankings, gold_map, [rec.qid for rec in scored], k, dense_note)

    lexical = [b for b in filtered if b.name in LEXICAL_BASELINES]
    strongest = max(lexical, key=lambda b: b.hits_at_k)
    # Wilson needs n >= 1. With nothing retained the bound is vacuous, and the verdict is
    # INCONCLUSIVE on the count alone.
    upper = wilson_interval(strongest.hits_at_k, strongest.n)[1] if strongest.n else 1.0
    gate = Gate(
        n_candidates=len(candidates),
        n_retained=len(retained),
        stale=len(stale),
        strongest=strongest.name,
        strongest_upper=upper,
    )
    outcome, reasons = verdict(gate)

    zero = [qid for qid in retained_ids if symbol_df.get(qid) == 0]
    positive = [qid for qid in retained_ids if symbol_df.get(qid, 0) > 0]
    dfs = [symbol_df[rec.qid] for rec in scored if rec.qid in symbol_df]
    return {
        "k": k,
        "split": GATE_SPLIT,
        "n_golden": len(golden),
        "n_unanswerable": len(in_split) - len(candidates),
        "n_candidates": len(candidates),
        "n_retained": len(retained),
        "n_dropped": len(dropped),
        "n_stale": len(stale),
        "stale": stale[:20],
        "retention_rate": gate.retention,
        "min_retained": MIN_RETAINED_DEV,
        "min_retention": MIN_RETENTION,
        "pool_size": len(pool),
        "gold_symbol_df": _df_summary(dfs),
        "symbol_df_strata": {
            "df_zero_share": len(zero) / len(retained) if retained else None,
            "df_zero": _stratum(rankings, gold_map, zero, k),
            "df_positive": _stratum(rankings, gold_map, positive, k),
        },
        "baselines": [_baseline_row(b) for b in filtered],
        "baselines_unfiltered": [_baseline_row(b) for b in unfiltered],
        "strongest_cheap_baseline": strongest.name,
        "strongest_cheap_recall_at_10": strongest.recall_at_10,
        "strongest_cheap_upper_95": upper,
        "grep_recall_at_10": next(b.recall_at_10 for b in filtered if b.name == "grep"),
        "ceiling": GREP_RECALL_AT_10_CEILING,
        "verdict": outcome,
        "verdict_reasons": reasons,
        "unresolved_anchors": unresolved_keys,
        "dropped": dropped[:200],
        "drop_reason_counts": dict(Counter(d["reason"].split(":", 1)[0] for d in dropped)),
    }


def _rankings(
    index: PoolIndex, records: Sequence[GoldenRecord], dense: DenseBaseline | None, k: int
) -> dict[str, dict[str, list[str]]]:
    """Each baseline's top k for every scored record.

    Lexical rows keep only documents that matched (`PoolIndex.matched`). Dense keeps its
    k nearest whatever their score: cosine similarity has no "no match".
    """
    lexical: dict[str, Callable[[str], np.ndarray]] = {
        "grep": index.grep.score,
        "bm25_default": lambda q: index.bm25_default.score(tokenize_default(q)),
        "bm25_ident": lambda q: index.bm25_ident.score(tokenize_identifier_aware(q)),
    }
    out = {
        name: {rec.qid: index.matched(score(rec.question), k) for rec in records}
        for name, score in lexical.items()
    }
    if dense is not None:
        out["dense"] = {
            rec.qid: [index.chunk_ids[i] for i in rank(dense.score(rec.question), k)]
            for rec in records
        }
    return out


def _baselines(
    rankings: dict[str, dict[str, list[str]]],
    gold: dict[str, set[str]],
    qids: Sequence[str],
    k: int,
    dense_note: str,
) -> list[BaselineResult]:
    rows = [
        evaluate(name, {qid: rankings[name][qid] for qid in qids}, gold, k)
        for name in (*LEXICAL_BASELINES, "dense")
        if name in rankings
    ]
    if "dense" not in rankings:
        note = dense_note or "no frozen vectors"
        rows.append(BaselineResult(name="dense", measured=False, note=note))
    return rows


def _stratum(
    rankings: dict[str, dict[str, list[str]]],
    gold: dict[str, set[str]],
    qids: Sequence[str],
    k: int,
) -> dict[str, Any]:
    """ADR-0002's thesis statistic: each baseline's recall@k on one df stratum."""
    if not qids:
        return {"n": 0, "recall_at_10": {}}
    recalls = {
        name: evaluate(name, {qid: ranked[qid] for qid in qids}, gold, k).recall_at_10
        for name, ranked in sorted(rankings.items())
    }
    return {"n": len(qids), "recall_at_10": recalls}


def _baseline_row(b: BaselineResult) -> dict[str, Any]:
    interval = list(wilson_interval(b.hits_at_k, b.n)) if b.measured and b.n else [None, None]
    return {
        "name": b.name,
        "measured": b.measured,
        "note": b.note,
        "recall_at_1": b.recall_at_1,
        "recall_at_10": b.recall_at_10,
        "mrr_at_10": b.mrr_at_10,
        "hits_at_10": b.hits_at_k,
        "n": b.n,
        "recall_at_10_wilson_95": interval,
    }


def _df_summary(dfs: Sequence[int]) -> dict[str, Any]:
    """How unique is the answer's primary key? Over every scored record's symbol."""
    if not dfs:
        return {
            "n": 0,
            "median": None,
            "mean": None,
            "min": None,
            "max": None,
            "frac_df_le_1": None,
        }
    return {
        "n": len(dfs),
        "median": statistics.median(dfs),
        "mean": statistics.fmean(dfs),
        "min": min(dfs),
        "max": max(dfs),
        "frac_df_le_1": sum(1 for d in dfs if d <= 1) / len(dfs),
    }


# -------------------------------------------------------------------------- report


def render_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    split = report["split"]

    def row(label: str, value: int, note: str = "") -> None:
        add(f"  {label:<23}{value:>8,}{note}")

    add("=" * 78)
    add("PACE LEAKAGE PROBE -- can a cheap baseline ace this benchmark?")
    add("=" * 78)
    add("")
    row("document pool", report["pool_size"], " documents")
    row("golden records loaded", report["n_golden"], f"   (the verdict reads {split} only)")
    row(f"{split} candidates", report["n_candidates"])
    row("retained after filters", report["n_retained"], f"   ({report['retention_rate']:.1%})")
    row("dropped", report["n_dropped"])
    for reason, count in sorted(report["drop_reason_counts"].items(), key=lambda x: (-x[1], x[0])):
        add(f"      - {reason:<38} {count:>6,}")
    if report["n_stale"]:
        row("STALE", report["n_stale"], "   stored leakage facts differ from this pool")
    if report["unresolved_anchors"]:
        add("")
        add(f"  UNRESOLVED ANCHORS on {len(report['unresolved_anchors'])} record(s) -- an ingest")
        add("  gap or a scoring bug, NEVER a retrieval miss:")
        for qid, keys in list(report["unresolved_anchors"].items())[:10]:
            add(f"      {qid}: {', '.join(keys[:6])}")
    add("")

    gsd = report["gold_symbol_df"]
    add("  gold symbol document frequency (how unique is the answer's primary key?)")
    if gsd["n"]:
        add(
            f"      median {gsd['median']}   mean {gsd['mean']:.2f}   "
            f"min {gsd['min']}   max {gsd['max']}   (n={gsd['n']:,} records)"
        )
        add(
            f"      fraction with df <= 1: {gsd['frac_df_le_1']:.1%}"
            "   (df==1 means the symbol IS the answer)"
        )
    else:
        add("      no scored record names a symbol")
    strata = report["symbol_df_strata"]
    if strata["df_zero_share"] is not None:
        add(
            "      retained records whose symbol is in no pool document: "
            f"{strata['df_zero_share']:.1%}"
        )
        for label in ("df_zero", "df_positive"):
            stratum = strata[label]
            recalls = ", ".join(f"{n} {r:.3f}" for n, r in stratum["recall_at_10"].items())
            add(f"        {label:<12} n={stratum['n']:<7,} recall@{report['k']}: {recalls or '-'}")
    add("")

    add(f"  BASELINES on the retained {split} set (the gate reads the strongest lexical row)")
    add(f"      {'baseline':<16}{'recall@1':>10}{'recall@10':>12}{'MRR@10':>10}   Wilson 95%")
    add(f"      {'-' * 70}")
    for b in report["baselines"]:
        if not b["measured"]:
            add(f"      {b['name']:<16}{'NOT MEASURED':>32}   {b['note']}")
            continue
        low, high = b["recall_at_10_wilson_95"]
        interval = "-" if low is None else f"[{low:.3f}, {high:.3f}]"
        strongest = b["name"] == report["strongest_cheap_baseline"]
        marker = "  <-- strongest lexical" if strongest else ""
        add(
            f"      {b['name']:<16}{b['recall_at_1']:>10.3f}{b['recall_at_10']:>12.3f}"
            f"{b['mrr_at_10']:>10.3f}   {interval}{marker}"
        )
    before = ", ".join(
        f"{b['name']} {b['recall_at_10']:.3f}"
        for b in report["baselines_unfiltered"]
        if b["measured"]
    )
    add(f"      recall@{report['k']} before the filters: {before or '-'}")
    add("")

    add("  GATE (ADR-0003 §6, fixed before any record existed)")
    add(
        f"      retained {split} records  {report['n_retained']:>8,}   "
        f"needs >= {report['min_retained']}"
    )
    add(
        f"      retention             {report['retention_rate']:>8.1%}   "
        f"needs >= {report['min_retention']:.0%}"
    )
    add(
        f"      upper bound           {report['strongest_cheap_upper_95']:>8.3f}   "
        f"needs <  {report['ceiling']:.2f}   ({report['strongest_cheap_baseline']})"
    )
    add("")

    outcome = report["verdict"]
    if outcome == PASS:
        add("  VERDICT: PASS")
        add("      No cheap lexical baseline can answer the retained dev set. Retrieval")
        add("      numbers measured on it mean something, and lift is quoted against")
        add(
            f"      '{report['strongest_cheap_baseline']}' "
            f"(recall@10 {report['strongest_cheap_recall_at_10']:.3f})."
        )
    elif outcome == FAIL:
        add("  VERDICT: FAIL  ***  THE BENCHMARK MEASURES NOTHING  ***")
        for reason in report["verdict_reasons"]:
            add(f"      - {reason}")
        add("      Every downstream number -- ablations, reranker lift, refusal precision --")
        add("      is invalid until this is fixed.")
    else:
        add("  VERDICT: INCONCLUSIVE  ***  NOT A PASS  ***")
        for reason in report["verdict_reasons"]:
            add(f"      - {reason}")
        add("      Nothing here is evidence that the benchmark is sound. A run that resolved")
        add("      no gold once printed PASS; that false green is what this verdict replaces.")
    add("")
    add(_meeting_summary(report))
    add("=" * 78)
    return "\n".join(lines)


def _meeting_summary(report: dict[str, Any]) -> str:
    """One paragraph, pasteable into a supervisor meeting or a progress report."""
    split, outcome = report["split"], report["verdict"]
    if outcome == INCONCLUSIVE:
        body = (
            f"No verdict on the {split} split: {'; '.join(report['verdict_reasons'])}. No "
            "claim about benchmark quality can be made from this run, and no retrieval "
            "number measured on it should be quoted."
        )
    else:
        rows = {b["name"]: b for b in report["baselines"]}
        lexical = ", ".join(f"{n} {rows[n]['recall_at_10']:.3f}" for n in LEXICAL_BASELINES)
        share = report["symbol_df_strata"]["df_zero_share"]
        df_clause = (
            f" {share:.0%} of retained questions name a symbol no pool document contains."
            if share is not None
            else ""
        )
        why = f": {'; '.join(report['verdict_reasons'])}" if report["verdict_reasons"] else ""
        body = (
            f"On {report['n_retained']:,} retained {split} queries "
            f"({report['retention_rate']:.0%} of {report['n_candidates']:,}) over a pool of "
            f"{report['pool_size']:,} documents, lexical recall@{report['k']} is {lexical}. "
            f"The strongest, '{report['strongest_cheap_baseline']}', has a Wilson 95% upper "
            f"bound of {report['strongest_cheap_upper_95']:.3f} against a ceiling of "
            f"{report['ceiling']:.2f}, so the verdict is {outcome}{why}.{df_clause}"
        )
    wrapped: list[str] = ["  SUMMARY (pasteable)", "  " + "-" * 48]
    line = "  "
    for token in body.split():
        if len(line) + len(token) + 1 > 78:
            wrapped.append(line)
            line = "  "
        line += ("" if line.strip() == "" else " ") + token
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


def _shown(path: Path) -> str:
    """A path as a committed report shows it: relative to the working directory when it
    is inside it, so the same run on another machine writes the same bytes."""
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


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

    report["golden_path"] = _shown(args.golden)
    report["frozen_embeddings_path"] = _shown(args.embeddings or fixture_path(args.model))
    print(render_report(report))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        args.json_out.write_text(rendered, encoding="utf-8")
        print(f"\nwrote {args.json_out}")

    if report["verdict"] == PASS:
        return 0
    if report["verdict"] == FAIL:
        return 1
    return 0 if args.allow_inconclusive else 2


if __name__ == "__main__":
    sys.exit(main())

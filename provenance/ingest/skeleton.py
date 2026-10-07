"""The week-2 walking skeleton: ONE command that touches every layer, thinly.

    ingest one file at one commit
      -> chunk it (tree-sitter)
        -> embed (the REAL provenance.retrieve embedder)
          -> index (pg_search BM25 + pgvector HNSW, in the production index shape)
            -> retrieve (BM25 + HNSW, fused with the REAL reciprocal_rank_fusion)
              -> synthesise (STUB: a templated citation string)
                -> score against ci_subset.jsonl via the REAL resolve_anchors()
    ...all inside ONE `pace.query` span whose trace_id is printed.

Phase-0 exit criterion
----------------------
"A hello-world request produces one complete trace, with trace_id in the
response." Three separate defects used to make that *look* satisfied while being
inert, and all three were of the same shape -- a list of candidate module paths,
tried in order, every one of them a miss, falling back to a local stand-in:

  * tracing:  the candidates named provenance.telemetry / provenance.obs.tracing
              / provenance.tracing. None exist. No tracer provider was ever
              installed, so the "trace id" printed was 32 ZEROS and not one span
              was exported.
  * embedder: the candidates named provenance.index.* / provenance.retrieval.* /
              provenance.embed. The package is provenance.retrieve, so the demo
              silently ran on a hashed bag-of-tokens stub.
  * fusion:   likewise; the demo silently ran on a private copy of RRF.

Every import in this module is therefore DIRECT. A direct import fails loudly,
once, at the right moment; a candidate list fails silently, forever, while the
command keeps printing a green-looking report. If you are tempted to reintroduce
reflection here because a sibling subtree "might get renamed" -- that rename is a
one-line fix, and this is the command that proves the system works.

Why a stub synthesiser
----------------------
An LLM dependency in week 2 is a distraction and a blocker: it needs a key, it
needs a budget, it fails in CI, and it hides which layer is actually broken. The
skeleton's job is to prove the *wiring* -- that a chunk_id produced by the chunker
survives into Postgres, comes back out of two different retrievers, fuses, and
resolves against a gold anchor. Swapping the stub for a real synthesiser in week
6 changes one function. The stages that are still stubs emit
``unimplemented_stage()``, so the trace itself says so and the eval harness can
refuse to score a scaffold run.

Deliberately NOT in this skeleton: SCIP, blame, the GitHub API, the agent loop,
the reranker, and function identity. Each of those is a week of its own; putting
any of them here means the skeleton does not run in week 2.
"""

from __future__ import annotations

import argparse
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from provenance.config import settings
from provenance.graph.tables import (
    BM25_FIELD_CONTENT,
    BM25_FIELD_LEXICAL,
    BM25_FIELD_QUALNAME,
    BM25_KEY_FIELD,
    COL_CHUNK_ID,
    halfvec_probe,
)
from provenance.ingest.git_walk import CatFileBatch, rev_parse
from provenance.ingest.scope import load_scope
from provenance.obs.otel import current_trace_id, setup_tracing, shutdown_tracing
from provenance.obs.spans import (
    ATTR_DEGRADED,
    PIPELINE_STAGES,
    SPAN_AGENT_STEP,
    SPAN_EMBED_QUERY,
    SPAN_FUSE_RRF,
    SPAN_PARSE_QUERY,
    SPAN_QUERY,
    SPAN_RERANK,
    SPAN_SEARCH_DENSE,
    SPAN_SEARCH_LEXICAL,
    SPAN_SYNTHESIZE,
    SPAN_VERIFY,
    stage,
    tool_span_name,
    unimplemented_stage,
)
from provenance.parse.chunker import Chunk, chunk_source, split_identifier
from provenance.retrieve.embedder import Embedder, FrozenEmbeddingMiss, get_embedder
from provenance.retrieve.fusion import DEFAULT_RRF_K, reciprocal_rank_fusion

log = logging.getLogger(__name__)

__all__ = [
    "DEMO_TABLE",
    "STUB_SYNTHESIS_VERSION",
    "DemoResult",
    "format_report",
    "main",
    "run_demo",
]

STUB_SYNTHESIS_VERSION = "stub-citation-v1"

# Week-2 scratch table. The real `chunks` table is created by 0001_core; the
# skeleton owns its own so it can run before an ingest has happened and be
# dropped afterwards. Its COLUMN NAMES are deliberately the production ones
# (imported from provenance.graph.tables) so this DDL is a faithful rehearsal of
# the real index shape rather than a second, subtly different schema.
DEMO_TABLE = "pace_skeleton_chunks"

DDL = f"""
CREATE TABLE IF NOT EXISTS {DEMO_TABLE} (
    {BM25_KEY_FIELD}     bigint GENERATED ALWAYS AS IDENTITY NOT NULL UNIQUE,
    {COL_CHUNK_ID}       text NOT NULL,
    repo                 text NOT NULL,
    path                 text NOT NULL,
    commit_sha           text NOT NULL,
    {BM25_FIELD_QUALNAME} text NOT NULL DEFAULT '',
    start_line           int  NOT NULL,
    end_line             int  NOT NULL,
    {BM25_FIELD_CONTENT}  text NOT NULL,
    {BM25_FIELD_LEXICAL}  text NOT NULL,
    embedding            vector({{dim}}) NOT NULL,
    PRIMARY KEY ({COL_CHUNK_ID}, commit_sha)
);
"""

# THREE DISTINCT COLUMNS, one tokenizer each. Measured on pg_search 0.25.7 and
# recorded verbatim in 0001_core:
#   * the same column may NOT appear twice under two casts
#       -> ERROR: indexed attribute content defined more than once
#   * a relation may have only ONE ParadeDB index
#   * pdb.literal emits the ENTIRE field as one token, so casting `content` to
#     literal makes every document a single enormous term -- useless.
# The previous version of this DDL indexed content::pdb.source_code AND
# content::pdb.literal, which is exactly error #1: `pace demo` died at index
# creation, i.e. the Phase-0 exit criterion could not run at all.
BM25_DDL = f"""
CREATE INDEX IF NOT EXISTS {DEMO_TABLE}_bm25 ON {DEMO_TABLE}
USING bm25 (
    {BM25_KEY_FIELD},
    ({BM25_FIELD_CONTENT}::pdb.source_code),
    ({BM25_FIELD_QUALNAME}::pdb.literal),
    ({BM25_FIELD_LEXICAL}::pdb.source_code)
)
WITH (key_field = '{BM25_KEY_FIELD}');
"""

# halfvec, exactly as 0004_vectors builds it. A probe that is not cast
# IDENTICALLY still returns correct rows -- from a sequential scan. That is a
# performance bug no test catches, so the demo rehearses the real cast.
HNSW_DDL = f"""
CREATE INDEX IF NOT EXISTS {DEMO_TABLE}_hnsw ON {DEMO_TABLE}
USING hnsw ((embedding::halfvec({{dim}})) halfvec_cosine_ops);
"""

#: Names of the two rank lists handed to reciprocal_rank_fusion. They appear in
#: FusedResult.ranks, which is the first thing you read when a result looks wrong.
SYSTEM_LEXICAL = "lexical"
SYSTEM_DENSE = "dense"


@dataclass(slots=True)
class DemoResult:
    trace_id: str
    commit_sha: str
    path: str
    query: str
    chunks: int
    indexed: int
    embedding_backend: str = ""
    bm25_hits: list[str] = field(default_factory=list)
    vector_hits: list[str] = field(default_factory=list)
    fused: list[str] = field(default_factory=list)
    answer: str = ""
    gold_records: int = 0
    resolved_anchors: int = 0
    recall_at_k: float | None = None
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------- embedding


def _stub_embed(texts: Sequence[str], dim: int) -> list[list[float]]:
    """Deterministic hashed bag-of-tokens. NEVER the default: it is not semantic.

    Exists so the wiring can be smoke-tested on a machine without the `ml` extra.
    Reaching it requires an explicit ``--allow-stub-embeddings``, and any caller
    that gets these vectors is told so, loudly, in the result notes and on the
    root span via ``pace.degraded``.
    """
    import hashlib

    out: list[list[float]] = []
    for text in texts:
        vec = [0.0] * dim
        for token in split_identifier(text) or [text]:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            vec[int.from_bytes(digest[:4], "big") % dim] += 1.0
        norm = math.sqrt(sum(value * value for value in vec)) or 1.0
        out.append([value / norm for value in vec])
    return out


def _rows(vectors: Any) -> list[list[float]]:
    """Normalise anything vector-shaped to plain float rows for the SQL literal."""
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    return [[float(value) for value in row] for row in array]


def _embed(
    embedder: Embedder,
    documents: Sequence[str],
    query: str,
    *,
    allow_stub: bool,
    notes: list[str],
) -> tuple[list[list[float]], list[float], str]:
    """Embed documents + query with the real backend, or (opt-in) the stub.

    The stub fallback is deliberately NOT taken for a FrozenEmbeddingMiss: a
    frozen-cache miss means the committed .npz is stale, and answering it with
    made-up vectors would turn a loud, fixable cache bug into a silently
    meaningless similarity number.
    """
    try:
        doc_vectors = _rows(embedder.encode_documents(list(documents)))
        query_vector = _rows(embedder.encode_query(query))[0]
    except FrozenEmbeddingMiss:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised unless stubbing was requested
        if not allow_stub:
            raise RuntimeError(
                f"embedding backend {type(embedder).__name__} failed ({exc}). "
                "Install the ml extra (`uv sync --extra ml`) or re-run with "
                "--allow-stub-embeddings. Refusing to substitute vectors silently."
            ) from exc
        log.warning("embedding backend unavailable (%s); using the stub", exc)
        notes.append(
            "STUB EMBEDDINGS: hashed bag-of-tokens, not semantic. "
            "Any similarity number from this run is meaningless."
        )
        dim = settings.embedding_dim
        return _stub_embed(list(documents), dim), _stub_embed([query], dim)[0], "stub"

    return doc_vectors, query_vector, type(embedder).__name__


# ------------------------------------------------------------------------ ingest


def _pick_path(explicit: str | None) -> str:
    if explicit:
        return explicit
    # No fallback. The frozen scope is the denominator of every recall figure, so a demo
    # that quietly runs over "the first .py under the corpus" still emits its 14 spans and
    # still reads as a passing walking skeleton -- over a file nobody chose. Let the
    # FileNotFoundError out; `pace scope` is the fix and its message says so.
    scope = load_scope()
    # Prefer a file with real logic over an __init__.py of re-exports.
    for rel in scope:
        if not rel.endswith("__init__.py"):
            return rel
    return scope[0]


def _read_at_commit(repo: Path, commit_sha: str, rel_path: str) -> str:
    """Read one blob through the batch process -- the same path the full walk uses."""
    oid = rev_parse(repo, f"{commit_sha}:{rel_path}")
    if oid is None:
        raise FileNotFoundError(f"{rel_path} not present at {commit_sha[:12]}")
    with CatFileBatch(repo) as batch:
        blob = batch.read(oid)
    if blob is None:
        raise FileNotFoundError(f"{rel_path} not present at {commit_sha[:12]}")
    return blob.decode("utf-8", errors="replace")


def _seen(emitted: list[str], name: str) -> str:
    """Record that a pipeline stage span was opened, and return its name.

    PIPELINE_STAGES is what the eval harness reads to decide a trace is complete.
    If a future edit drops a stage from run_demo, the demo must fail loudly rather
    than emit a trace that merely looks complete -- an incomplete trace scored as
    a complete one is the same failure mode as the 32-zero trace id, just later.
    """
    emitted.append(name)
    return name


# ------------------------------------------------------------------------- demo


def run_demo(
    path: str | None = None,
    *,
    commit: str = "HEAD",
    top_k: int = 5,
    query: str | None = None,
    allow_stub_embeddings: bool = False,
    keep_table: bool = False,
) -> DemoResult:
    """Run the whole thin slice and return what each layer produced."""
    # Installs the real tracer provider + OTLP exporter. Idempotent, so calling
    # it here and from the API lifespan is safe. Without it every span is a
    # non-recording no-op and trace_id is 32 zeros.
    setup_tracing()

    repo = Path(settings.corpus_path)
    notes: list[str] = []
    emitted: list[str] = []

    with stage(
        SPAN_QUERY,
        **{
            "pace.chunking_strategy_version": settings.chunking_strategy_version,
            "pace.embedding_model_version": settings.embedding_model_version,
        },
    ) as root_span:
        trace_id = current_trace_id()
        if trace_id is None:
            # 32 zeros used to be printed here for months. Never let that be
            # mistaken for a trace again: no provider means no exit criterion.
            raise RuntimeError(
                "no recording span: setup_tracing() did not install a tracer provider, "
                "so nothing would be exported and the trace_id would be 32 zeros. "
                "Check provenance.obs.otel and the opentelemetry-sdk install."
            )

        # ---- ingest ----------------------------------------------------
        with stage(tool_span_name("ingest.read_blob")) as span:
            rel_path = _pick_path(path)
            commit_sha = rev_parse(repo, commit)
            if commit_sha is None:
                raise RuntimeError(f"cannot resolve {commit!r} in {repo}")
            source = _read_at_commit(repo, commit_sha, rel_path)
            span.set_attribute("pace.path", rel_path)
            span.set_attribute("pace.commit_sha", commit_sha)
            span.set_attribute("pace.bytes", len(source))

        # ---- chunk -----------------------------------------------------
        with stage(tool_span_name("parse.chunk")) as span:
            chunks = chunk_source(source, rel_path)
            span.set_attribute("pace.chunks", len(chunks))
            span.set_attribute("pace.chunking_strategy_version", settings.chunking_strategy_version)

        # ---- parse query -----------------------------------------------
        with stage(_seen(emitted, SPAN_PARSE_QUERY)) as span:
            effective_query = query or _derive_query(chunks)
            span.set_attribute("pace.query_text", effective_query)
        root_span.set_attribute("pace.query_text", effective_query)

        # ---- embed -----------------------------------------------------
        with stage(_seen(emitted, SPAN_EMBED_QUERY)) as span:
            embedder = get_embedder()
            vectors, query_vector, backend = _embed(
                embedder,
                [c.content for c in chunks],
                effective_query,
                allow_stub=allow_stub_embeddings,
                notes=notes,
            )
            span.set_attribute("pace.embedding_backend", backend)
            span.set_attribute("pace.embedding_model_version", settings.embedding_model_version)
            if backend == "stub":
                span.set_attribute(ATTR_DEGRADED, True)
                root_span.set_attribute(ATTR_DEGRADED, True)
            if len(query_vector) != settings.embedding_dim:
                raise RuntimeError(
                    f"embedding dim {len(query_vector)} != settings.embedding_dim "
                    f"{settings.embedding_dim}; the HNSW column would silently reject rows"
                )

        # ---- index -----------------------------------------------------
        with stage(tool_span_name("index.write")) as span:
            indexed = _index(chunks, vectors, commit_sha)
            span.set_attribute("pace.rows", indexed)

        # ---- retrieve --------------------------------------------------
        with stage(_seen(emitted, SPAN_SEARCH_LEXICAL)) as span:
            bm25 = _bm25_search(effective_query, commit_sha, top_k * 2)
            span.set_attribute("pace.hits", len(bm25))
        with stage(_seen(emitted, SPAN_SEARCH_DENSE)) as span:
            vector_hits = _vector_search(query_vector, commit_sha, top_k * 2)
            span.set_attribute("pace.hits", len(vector_hits))
        with stage(_seen(emitted, SPAN_FUSE_RRF)) as span:
            fused_results = reciprocal_rank_fusion(
                {SYSTEM_LEXICAL: bm25, SYSTEM_DENSE: vector_hits},
                k=DEFAULT_RRF_K,
                limit=top_k,
            )
            fused = [item.chunk_id for item in fused_results]
            span.set_attribute("pace.fused", len(fused))
            span.set_attribute("pace.rrf_k", DEFAULT_RRF_K)

        # The two stages the skeleton does not implement. They are emitted, not
        # skipped: the trace shape is fixed now, each span is replaced in place
        # later, and pace.unimplemented is what stops a scaffold run scoring.
        with unimplemented_stage(_seen(emitted, SPAN_RERANK)):
            pass
        with unimplemented_stage(_seen(emitted, SPAN_AGENT_STEP)):
            pass

        by_id = {c.chunk_id: c for c in chunks}

        # ---- synthesise (STUB) ----------------------------------------
        with unimplemented_stage(_seen(emitted, SPAN_SYNTHESIZE)) as span:
            top = [by_id[cid] for cid in fused if cid in by_id]
            answer = _stub_synthesise(effective_query, top, commit_sha)
            span.set_attribute("pace.prompt_version", STUB_SYNTHESIS_VERSION)
            span.set_attribute("pace.llm_calls", 0)

        with unimplemented_stage(_seen(emitted, SPAN_VERIFY)) as span:
            span.set_attribute("pace.citations", len(top))

        # ---- score -----------------------------------------------------
        with stage(tool_span_name("eval.score")) as span:
            gold_count, resolved, recall = _score(fused, notes)
            span.set_attribute("pace.gold_records", gold_count)
            span.set_attribute("pace.resolved_anchors", resolved)
            if recall is not None:
                span.set_attribute("pace.recall_at_k", recall)

        missing = [name for name in PIPELINE_STAGES if name not in emitted]
        if missing:
            raise RuntimeError(
                f"incomplete trace: {missing} were never opened. The Phase-0 exit "
                "criterion is ONE COMPLETE trace, and PIPELINE_STAGES is how the "
                "eval harness checks it."
            )
        root_span.set_attribute("pace.pipeline_stages", len(emitted))

        if not keep_table:
            _cleanup(commit_sha)

        return DemoResult(
            trace_id=trace_id,
            commit_sha=commit_sha,
            path=rel_path,
            query=effective_query,
            chunks=len(chunks),
            indexed=indexed,
            embedding_backend=backend,
            bm25_hits=bm25[:top_k],
            vector_hits=vector_hits[:top_k],
            fused=fused,
            answer=answer,
            gold_records=gold_count,
            resolved_anchors=resolved,
            recall_at_k=recall,
            notes=notes,
        )


def _derive_query(chunks: Sequence[Chunk]) -> str:
    """Query built from the biggest chunk's qualified name.

    Split into parts on purpose: `pdb.source_code` would split it anyway, so this
    is what the BM25 side actually sees, and it makes the tokenizer behaviour
    visible in the demo output instead of hidden behind a hand-picked string.
    """
    named = [c for c in chunks if c.qualified_name] or list(chunks)
    target = max(named, key=lambda c: c.token_estimate)
    parts = split_identifier((target.qualified_name or target.path).replace(".", "_"))
    return " ".join(parts[:8]) or "airflow"


# ------------------------------------------------------------------------ storage


def _index(chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]], commit_sha: str) -> int:
    from provenance.graph.db import connection

    dim = str(settings.embedding_dim)
    with connection() as conn, conn.cursor() as cur:
        cur.execute(DDL.replace("{dim}", dim))
        cur.execute(BM25_DDL)
        cur.execute(HNSW_DDL.replace("{dim}", dim))
        cur.execute(f"DELETE FROM {DEMO_TABLE} WHERE commit_sha = %s", (commit_sha,))
        rows = 0
        for chunk, vector in zip(chunks, vectors, strict=True):
            cur.execute(
                f"""
                INSERT INTO {DEMO_TABLE}
                    ({COL_CHUNK_ID}, repo, path, commit_sha, {BM25_FIELD_QUALNAME},
                     start_line, end_line, {BM25_FIELD_CONTENT}, {BM25_FIELD_LEXICAL}, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                ON CONFLICT ({COL_CHUNK_ID}, commit_sha) DO NOTHING
                """,
                (
                    chunk.chunk_id,
                    chunk.repo,
                    chunk.path,
                    commit_sha,
                    chunk.qualified_name or "",
                    chunk.start_line,
                    chunk.end_line,
                    chunk.content,
                    # The space-joined token bag, from the ONE place that joins
                    # it. The bm25 index reads this column; leave it empty and
                    # the joined-form match ('normaliseurl') silently stops
                    # working while every other query still looks fine.
                    chunk.lexical_blob,
                    _vector_literal(vector),
                ),
            )
            rows += cur.rowcount or 0
        conn.commit()
    return rows


def _vector_literal(vector: Sequence[float]) -> str:
    # Passed as a text literal cast to vector so this works whether or not the
    # pool has run register_vector on this particular connection.
    return "[" + ",".join(f"{float(v):.6g}" for v in vector) + "]"


def _bm25_search(query: str, commit_sha: str, limit: int) -> list[str]:
    """Cross-field OR over the three bm25 fields, scored by paradedb.score().

    paradedb.score() MUST be passed the index's key_field (the row-id surrogate),
    never chunk_id: passing anything else is an error, and the whole reason the
    surrogate column exists.
    """
    from provenance.graph.db import connection

    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {COL_CHUNK_ID}
              FROM {DEMO_TABLE}
             WHERE commit_sha = %s
               AND ({BM25_FIELD_CONTENT} @@@ %s
                    OR {BM25_FIELD_LEXICAL} @@@ %s
                    OR {BM25_FIELD_QUALNAME} @@@ %s)
             ORDER BY paradedb.score({BM25_KEY_FIELD}) DESC
             LIMIT %s
            """,
            (commit_sha, query, query, query, limit),
        )
        return [row[0] for row in cur.fetchall()]


def _vector_search(query_vector: Sequence[float], commit_sha: str, limit: int) -> list[str]:
    from provenance.graph.db import connection

    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {COL_CHUNK_ID}
              FROM {DEMO_TABLE}
             WHERE commit_sha = %s
             ORDER BY {halfvec_probe(settings.embedding_dim)}
             LIMIT %s
            """,
            (commit_sha, _vector_literal(query_vector), limit),
        )
        return [row[0] for row in cur.fetchall()]


def _cleanup(commit_sha: str) -> None:
    from provenance.graph.db import connection

    with connection() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {DEMO_TABLE} WHERE commit_sha = %s", (commit_sha,))
        conn.commit()


# ------------------------------------------------------------- stub synthesiser


def _stub_synthesise(query: str, chunks: Sequence[Chunk], commit_sha: str) -> str:
    """Templated citation string. No LLM, no API key, no network, no cost.

    It still exercises the shape the real synthesiser must produce: an answer that
    is nothing but citations, or an explicit refusal when there is nothing to cite.
    """
    if not chunks:
        return (
            f"REFUSED: nothing retrieved for {query!r}. The rationale for this code "
            f"was not found in the indexed corpus at {commit_sha[:12]}."
        )
    lines = [f"[stub {STUB_SYNTHESIS_VERSION}] Evidence for {query!r} at {commit_sha[:12]}:"]
    for rank, chunk in enumerate(chunks, start=1):
        name = chunk.qualified_name or "<module>"
        lines.append(
            f"  {rank}. {chunk.path}:{chunk.start_line}-{chunk.end_line} "
            f"({chunk.kind} {name}) [chunk_id={chunk.chunk_id[:12]}]"
        )
    lines.append("  (no LLM was called; week 6 replaces this function and nothing else)")
    return "\n".join(lines)


# -------------------------------------------------------------------- scoring


def _score(retrieved: Sequence[str], notes: list[str]) -> tuple[int, int, float | None]:
    """Score against ci_subset.jsonl, resolving gold anchors to chunk_ids.

    ONE loader (provenance.eval.schema.load_golden_set) and ONE resolver
    (provenance.eval.anchors.resolve_anchors), both imported directly. A private
    reimplementation of either is how the golden set came to parse as empty
    everywhere else in this tree: zero gold, recall 0.0, verdict PASS for
    entirely the wrong reason.

    Gold is keyed on commit_sha / pr_number / (path, qualified_name) -- NEVER on a
    chunk_id -- so this resolution step is what lets the golden set survive a
    chunker change. Anchors resolve against the REAL `chunks` table; the ids line
    up with the demo's because chunk_id is content-addressed. If the corpus has
    not been ingested there is nothing to resolve against: say so, do not invent
    a number.
    """
    from provenance.eval.anchors import resolve_anchors
    from provenance.eval.runner import DEFAULT_GOLDEN
    from provenance.eval.schema import load_golden_set
    from provenance.graph.db import connection
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    records = load_golden_set(DEFAULT_GOLDEN)
    if not records:
        notes.append(f"golden set at {DEFAULT_GOLDEN} is empty; retrieval ran but nothing scored")
        return 0, 0, None

    retrieved_set = set(retrieved)
    hits = 0
    scored = 0
    resolved_total = 0
    try:
        with connection() as conn:
            repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
            for record in records:
                resolved = resolve_anchors(record, conn, repo_id=repo_id)
                gold = resolved.required_chunk_ids | resolved.supporting_chunk_ids
                resolved_total += len(gold)
                if not gold:
                    continue
                scored += 1
                if retrieved_set & gold:
                    hits += 1
    except UnknownRepository as exc:
        notes.append(f"{exc}")
        return len(records), 0, None

    if scored == 0:
        notes.append(
            "0 gold anchors resolved to chunk_ids -- expected when the skeleton "
            "ingests a single file that the golden set does not cover"
        )
        return len(records), resolved_total, None
    return len(records), resolved_total, hits / scored


# --------------------------------------------------------------------- reporting


def format_report(result: DemoResult) -> str:
    lines = [
        f"trace_id      {result.trace_id}",
        f"commit        {result.commit_sha[:12]}",
        f"file          {result.path}",
        f"query         {result.query!r}",
        f"embedder      {result.embedding_backend}",
        f"chunks        {result.chunks} produced, {result.indexed} indexed",
        f"bm25 top      {[c[:12] for c in result.bm25_hits]}",
        f"hnsw top      {[c[:12] for c in result.vector_hits]}",
        f"rrf top{len(result.fused):<2}    {[c[:12] for c in result.fused]}",
        f"gold          {result.gold_records} records, {result.resolved_anchors} anchors resolved",
        f"recall@k      {result.recall_at_k if result.recall_at_k is not None else 'not scored'}",
        "",
        result.answer,
    ]
    lines.extend(f"NOTE: {note}" for note in result.notes)
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pace demo",
        description="Walking skeleton: one request, one complete trace, trace_id in the output.",
    )
    parser.add_argument("--path", default=None, help="repo-relative file to ingest")
    parser.add_argument("--commit", default="HEAD", help="revision to read the file at")
    parser.add_argument("--top-k", type=int, default=5, help="results after fusion")
    parser.add_argument("--query", default=None, help="override the derived query")
    parser.add_argument(
        "--allow-stub-embeddings",
        action="store_true",
        help="fall back to hashed bag-of-tokens vectors if the real backend is missing",
    )
    parser.add_argument(
        "--keep-table", action="store_true", help=f"leave rows in {DEMO_TABLE} for inspection"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Thin CLI adapter for `pace demo`. Prints the report and flushes the trace."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = run_demo(
            args.path,
            commit=args.commit,
            top_k=args.top_k,
            query=args.query,
            allow_stub_embeddings=args.allow_stub_embeddings,
            keep_table=args.keep_table,
        )
        print(format_report(result))
        return 0
    finally:
        # BatchSpanProcessor exports on a timer. A CLI run that finishes in
        # 200ms exits before the first export tick and drops the whole trace on
        # the floor -- the exit criterion would pass locally and show nothing in
        # Jaeger. Flush before returning, on the error path too.
        shutdown_tracing()

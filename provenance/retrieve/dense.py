"""Dense retrieval: HNSW k-NN over pgvector, plus exact brute-force ground truth.

THE FAILURE THIS MODULE PREVENTS: quoting a recall number that was never
measured. An approximate index has a recall knob (ef_search) whose default was
chosen by the library author for someone else's data. Without an exact-search
mode there is nothing to measure recall AGAINST, so "recall@10 = 0.91" would be
a number about the index compared to itself.

`exact_search()` earns its place twice over:

  1. It is the recall baseline for index tuning. `measured_recall()` sweeps
     ef_search against it so the operating point is chosen at a visible knee,
     not accepted as a default.
  2. It is the T5 determinism invariant for the ingest gate. HNSW is a graph
     walk: its results can shift with insertion order, with a VACUUM, with
     parallel workers. Brute force cannot. So the gate's "same corpus, same
     query, same ranking" assertion runs in exact mode; anything else would make
     the gate flap for reasons that are not regressions.

--------------------------------------------------------------------------
WHERE THE VECTORS ACTUALLY LIVE
--------------------------------------------------------------------------
There is NO embedding column on `chunks`. Vectors live in their own blue/green
pair, `embeddings_code_blue` / `embeddings_code_green` (0004_vectors), keyed on
(chunk_id, model_version). Readers resolve the LOGICAL alias:

    SELECT physical_table FROM index_aliases WHERE alias = 'code_current';

and join back to `chunks` on chunk_id for metadata and filtering. Resolving the
alias per query is the entire point of the blue/green scheme: a rebuild fills
the idle colour and the flip is two UPDATEs in one transaction, with no code
change, no deploy and no downtime. Hardcode a physical table here and the next
flip silently serves stale vectors from the colour nobody is writing to.

--------------------------------------------------------------------------
STORAGE: vector on the heap, halfvec in the index
--------------------------------------------------------------------------
The embedding column is `vector(384)`; the HNSW index is built over the
`halfvec(384)` cast:

    CREATE INDEX embeddings_code_blue_hnsw ON embeddings_code_blue
    USING hnsw ((embedding::halfvec(384)) halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

fp16 halves index memory, which matters on a box where WSL2 sees ~3.7 GB. The
exact vector stays on the heap so brute force is genuinely exact ground truth.
Every query MUST apply the SAME cast to the probe or the planner will not use
the index -- it silently seq-scans, returns the correct rows, and the only
symptom is that things got slow. That is why the ORDER BY fragment comes from
`tables.halfvec_probe(dim)` and is never typed by hand here.

--------------------------------------------------------------------------
pgvector POST-FILTERS. THIS IS THE THING PEOPLE GET WRONG.
--------------------------------------------------------------------------
A query like

    SELECT ... FROM embeddings_code_blue e JOIN chunks c USING (chunk_id)
    WHERE c.repo_id = 1
    ORDER BY e.embedding::halfvec(384) <=> %s::halfvec(384) LIMIT 10

does NOT search "the nearest neighbours among rows where repo_id = 1". HNSW
returns its ef_search candidates from the WHOLE index, and the WHERE clause is
applied to those candidates AFTERWARDS. If the filter is selective, you can ask
for 10 and get 2, or 0 -- the index never looked at the rows you wanted.

`hnsw.iterative_scan` (pgvector >= 0.8, we run 0.8.4) does NOT fix this by
pre-filtering. It re-scans: it keeps pulling further batches from the index
until enough rows survive the filter or `hnsw.max_scan_tuples` is hit. That is
post-filter-with-retry. It rescues recall on moderately selective filters at the
cost of latency, and it degrades badly as selectivity increases -- with a filter
matching 0.1% of rows it will scan enormous numbers of tuples to find ten.

THE GENUINE PRE-FILTER IS A PARTIAL INDEX PER VISIBILITY GROUP, and 0004 builds
exactly one:

    CREATE INDEX embeddings_code_blue_hnsw_public ON embeddings_code_blue
    USING hnsw ((embedding::halfvec(384)) halfvec_cosine_ops)
    WHERE visibility_group = 'public';

Now the graph itself contains only matching rows, so the k-NN walk is inside the
filtered set and k results means k results. It costs one index per group and
only works for a small, fixed, low-cardinality set of groups -- which is exactly
what a visibility dimension is, and exactly what `repo_id`, `language` or
`path LIKE` are not. Those stay post-filters, and callers must be told they may
get back fewer than `limit` rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import psycopg

from provenance.config import settings
from provenance.graph.db import connection
from provenance.graph.repos import resolve_repo_id, split_repo_slug
from provenance.graph.tables import (
    ALIAS_CODE,
    CHUNKS,
    COL_CHUNK_ID,
    COL_LANGUAGE,
    COL_REPO_ID,
    COL_TOMBSTONE,
    EMBED_CODE_TABLES,
    INDEX_ALIASES,
    halfvec_probe,
)

__all__ = [
    "DEFAULT_EF_SEARCH",
    "DenseResult",
    "NoCurrentEmbeddingIndex",
    "RecallPoint",
    "dense_search",
    "exact_search",
    "format_recall_table",
    "measured_recall",
    "resolve_code_embedding_table",
]

# Not a considered value yet -- a starting point. Replace it with the knee that
# measured_recall() actually shows on this corpus, and record the plot.
DEFAULT_EF_SEARCH = 100


class NoCurrentEmbeddingIndex(RuntimeError):
    """No physical embedding table is promoted to the `code_current` alias.

    Raised rather than falling back to a hardcoded colour: a fallback would make
    a half-finished blue/green flip look like a working index serving stale
    vectors, which is indistinguishable from "retrieval got worse" in the eval.
    """


@dataclass(frozen=True, slots=True)
class DenseResult:
    chunk_id: str
    distance: float  # cosine distance, 0 = identical
    rank: int  # 1-based

    @property
    def similarity(self) -> float:
        return 1.0 - self.distance


@dataclass(frozen=True, slots=True)
class RecallPoint:
    ef_search: int
    recall_at_k: float
    mean_latency_ms: float


def resolve_code_embedding_table(conn: psycopg.Connection) -> str:
    """Resolve the `code_current` alias to its physical table name.

    Called once per search, inside the same transaction as the search, so a flip
    that commits mid-request is either fully visible or fully invisible -- never
    a probe against blue and a metadata join against green.

    The returned name is interpolated into SQL as an identifier, so it is
    checked against the compile-time allowlist in tables.EMBED_CODE_TABLES.
    `index_aliases` is an ordinary writable table; without this check a row
    written there becomes an injected identifier. Do not relax it into a regex.
    """
    row = conn.execute(
        f"SELECT physical_table FROM {INDEX_ALIASES} WHERE alias = %s",
        (ALIAS_CODE,),
    ).fetchone()
    if row is None:
        raise NoCurrentEmbeddingIndex(
            f"no row in {INDEX_ALIASES} carries alias {ALIAS_CODE!r}. "
            "Build a colour and promote it before querying; an empty result "
            "here would otherwise look exactly like an empty corpus."
        )
    table = row[0] if isinstance(row, tuple) else row["physical_table"]
    if table not in EMBED_CODE_TABLES:
        raise NoCurrentEmbeddingIndex(
            f"alias {ALIAS_CODE!r} points at {table!r}, which is not one of "
            f"{EMBED_CODE_TABLES}. Refusing to interpolate an unknown identifier."
        )
    return str(table)


def _resolve_repo(
    conn: psycopg.Connection,
    repo: str | None,
    repo_id: int | None,
) -> int | None:
    """`"apache/airflow"` -> bigint, or pass an already-resolved id straight back.

    chunks.repo_id is a bigint FK. Passing the slug through raises
    `invalid input syntax for type bigint` -- but only once the query runs.
    """
    if repo_id is not None:
        return int(repo_id)
    if repo is None:
        return None
    owner, name = split_repo_slug(repo)
    return resolve_repo_id(conn, owner, name)


def _filters(
    *,
    repo_id: int | None,
    path_prefix: str | None,
    lang: str | None,
    model_version: str | None,
    visibility_group: str | None,
    include_tombstoned: bool,
) -> tuple[str, list[object]]:
    """Build the WHERE tail plus its POSITIONAL parameters, in SQL order.

    Positional (%s) parameters are forced on us by halfvec_probe(), which the
    probe must go through -- see the module docstring. The consequence is that
    the parameter LIST must be assembled in exactly the order the fragments
    appear in the statement: probe, filters, probe again, limit. Every caller
    below does that in one place; do not scatter it.
    """
    clauses: list[str] = []
    params: list[object] = []

    # Mixed-model neighbourhoods produce cosine scores that are arithmetically
    # valid and semantically meaningless. model_version is part of the PK, so a
    # partial re-embed CAN leave two models in one table; this clause is what
    # stops the search from silently ranking across both.
    if model_version:
        clauses.append("AND e.model_version = %s")
        params.append(model_version)
    if visibility_group:
        # The one genuine pre-filter: 0004 builds a partial HNSW index on
        # visibility_group = 'public', so this clause can be answered inside the
        # graph rather than after it.
        clauses.append("AND e.visibility_group = %s")
        params.append(visibility_group)
    if repo_id is not None:
        clauses.append(f"AND c.{COL_REPO_ID} = %s")
        params.append(repo_id)
    if path_prefix:
        clauses.append("AND c.path LIKE %s")
        params.append(f"{path_prefix}%")
    if lang:
        clauses.append(f"AND c.{COL_LANGUAGE} = %s")
        params.append(lang)
    if not include_tombstoned:
        # Deleted code stays queryable ("why was this removed"), but it is not
        # the default retrieval surface: chunks_live_idx is partial on exactly
        # this predicate.
        clauses.append(f"AND c.{COL_TOMBSTONE} IS NULL")
    return "\n  ".join(clauses), params


def _as_literal(query_vector: Sequence[float] | np.ndarray) -> str:
    v = np.asarray(query_vector, dtype=np.float32).ravel()
    if v.shape[0] != settings.embedding_dim:
        raise ValueError(f"query vector has {v.shape[0]} dims, expected {settings.embedding_dim}")
    return "[" + ",".join(f"{x:.7g}" for x in v.tolist()) + "]"


def dense_search(
    query_vector: Sequence[float] | np.ndarray,
    limit: int = 50,
    *,
    ef_search: int = DEFAULT_EF_SEARCH,
    iterative_scan: bool = False,
    repo: str | None = None,
    repo_id: int | None = None,
    path_prefix: str | None = None,
    lang: str | None = None,
    model_version: str | None = None,
    visibility_group: str | None = "public",
    include_tombstoned: bool = False,
) -> list[DenseResult]:
    """Approximate k-NN via the HNSW index on the currently promoted colour.

    ``ef_search`` is the search-time beam width: bigger means more of the graph
    is explored, higher recall, more latency. It must be >= limit or the index
    cannot return `limit` rows.

    If any of ``repo``/``path_prefix``/``lang`` is given, read the POST-FILTER
    section of the module docstring: you may get back fewer than ``limit`` rows,
    and that is the index working as designed, not a bug. ``iterative_scan=True``
    turns on post-filter-with-retry, which trades latency for rows recovered.
    ``visibility_group`` is the one filter that is a real pre-filter.

    ``repo`` takes the ``owner/name`` slug and is resolved to the bigint
    ``repo_id`` here; pass ``repo_id`` directly if you already have it.
    """
    if ef_search < limit:
        ef_search = limit
    dim = settings.embedding_dim
    probe = halfvec_probe(dim)  # "embedding::halfvec(d) <=> %s::halfvec(d)"
    literal = _as_literal(query_vector)

    with connection() as conn, conn.cursor() as cur:
        table = resolve_code_embedding_table(conn)
        rid = _resolve_repo(conn, repo, repo_id)
        filter_sql, filter_params = _filters(
            repo_id=rid,
            path_prefix=path_prefix,
            lang=lang,
            model_version=model_version or settings.embedding_model_version,
            visibility_group=visibility_group,
            include_tombstoned=include_tombstoned,
        )
        # `embedding` is unqualified because halfvec_probe() emits it that way,
        # and it is unambiguous: chunks has no embedding column (that is finding
        # #3). If one is ever added, this becomes a loud "column reference is
        # ambiguous" rather than a wrong answer -- leave it that way.
        sql = f"""
SELECT c.{COL_CHUNK_ID}, ({probe}) AS distance
FROM {table} e
JOIN {CHUNKS} c ON c.{COL_CHUNK_ID} = e.{COL_CHUNK_ID}
WHERE TRUE
  {filter_sql}
ORDER BY {probe}
LIMIT %s
"""
        # set_config(..., is_local => true) is SET LOCAL: scoped to this
        # transaction, so a pooled connection is never handed back to another
        # caller carrying our tuning. It is NOT written as `SET LOCAL
        # hnsw.ef_search = %s`: SET is a utility statement and cannot take a
        # bound parameter, so that form dies with a syntax error at $1 the first
        # time this path is exercised against a real server.
        cur.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(ef_search),))
        if iterative_scan:
            cur.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
        cur.execute(sql, [literal, *filter_params, literal, limit])
        rows = cur.fetchall()

    return [
        DenseResult(chunk_id=r[0], distance=float(r[1]), rank=i)
        for i, r in enumerate(rows, start=1)
    ]


def exact_search(
    query_vector: Sequence[float] | np.ndarray,
    limit: int = 50,
    *,
    repo: str | None = None,
    repo_id: int | None = None,
    path_prefix: str | None = None,
    lang: str | None = None,
    model_version: str | None = None,
    visibility_group: str | None = "public",
    include_tombstoned: bool = False,
) -> list[DenseResult]:
    """Brute-force k-NN. Ground truth, and the determinism invariant.

    Forces a sequential scan by disabling index scans for the transaction, so
    every embedding row is compared. On the full airflow corpus this is slow
    (seconds, not milliseconds) -- that is expected and is why it is not a
    serving path.

    Two properties make it worth the cost:
      * it is EXACT, so recall@k of the approximate index is measurable;
      * it is DETERMINISTIC, so the ingest gate's T5 "same corpus, same query,
        same ranking" assertion has something stable to assert about. Ties break
        on chunk_id to remove the last source of ordering nondeterminism.

    The halfvec cast is kept even though the index is deliberately unused, so
    ground truth is computed in the SAME arithmetic as the thing it is ground
    truth FOR. Comparing an fp32 baseline against an fp16 index would fold the
    precision difference into the reported recall number.
    """
    dim = settings.embedding_dim
    probe = halfvec_probe(dim)
    literal = _as_literal(query_vector)

    with connection() as conn, conn.cursor() as cur:
        table = resolve_code_embedding_table(conn)
        rid = _resolve_repo(conn, repo, repo_id)
        filter_sql, filter_params = _filters(
            repo_id=rid,
            path_prefix=path_prefix,
            lang=lang,
            model_version=model_version or settings.embedding_model_version,
            visibility_group=visibility_group,
            include_tombstoned=include_tombstoned,
        )
        # ORDER BY the output name, not a second copy of the probe: in exact
        # mode we do NOT want the index, so there is nothing to keep the
        # expression shape identical for, and one probe parameter is one fewer
        # thing to get out of order.
        sql = f"""
SELECT c.{COL_CHUNK_ID}, ({probe}) AS distance
FROM {table} e
JOIN {CHUNKS} c ON c.{COL_CHUNK_ID} = e.{COL_CHUNK_ID}
WHERE TRUE
  {filter_sql}
ORDER BY distance, c.{COL_CHUNK_ID}
LIMIT %s
"""
        # These take no parameter, so the plain SET LOCAL form is fine here.
        # Do NOT "simplify" the ef_search call above to match this shape.
        cur.execute("SET LOCAL enable_indexscan = off")
        cur.execute("SET LOCAL enable_indexonlyscan = off")
        # bitmap scans can still reach the HNSW index on some plans; close the door.
        cur.execute("SET LOCAL enable_bitmapscan = off")
        cur.execute(sql, [literal, *filter_params, limit])
        rows = cur.fetchall()
    return [
        DenseResult(chunk_id=r[0], distance=float(r[1]), rank=i)
        for i, r in enumerate(rows, start=1)
    ]


def measured_recall(
    query_vectors: Iterable[Sequence[float] | np.ndarray],
    k: int = 10,
    ef_values: Sequence[int] = (10, 20, 40, 60, 80, 100, 150, 200, 400),
    *,
    repo: str | None = None,
    repo_id: int | None = None,
) -> list[RecallPoint]:
    """Sweep ef_search against exact ground truth. Returns the recall curve.

    recall@k = |approx_top_k ∩ exact_top_k| / k, averaged over queries. Set
    overlap, not rank correlation: for RAG what matters is whether the right
    chunk reached the fusion stage at all.

    Run it over the GOLDEN SET's queries, not random ones. Recall on random
    vectors is a property of the index; recall on real queries is a property of
    the system, and only the second one is allowed near the report. Pick the
    knee -- where recall stops climbing but latency keeps climbing -- and write
    the chosen ef_search plus this table into the evaluation chapter. A default
    accepted silently is a paragraph you cannot write.

    Ground truth is computed once per query and reused across all ef values, so
    the expensive half runs len(queries) times, not len(queries) * len(ef).
    """
    import time

    vectors = [np.asarray(v, dtype=np.float32).ravel() for v in query_vectors]
    if not vectors:
        return []

    truth: list[set[str]] = [
        {r.chunk_id for r in exact_search(v, limit=k, repo=repo, repo_id=repo_id)} for v in vectors
    ]

    points: list[RecallPoint] = []
    for ef in ef_values:
        hits = 0.0
        elapsed = 0.0
        for vec, gold in zip(vectors, truth, strict=True):
            t0 = time.perf_counter()
            approx = dense_search(vec, limit=k, ef_search=ef, repo=repo, repo_id=repo_id)
            elapsed += (time.perf_counter() - t0) * 1000.0
            if gold:
                hits += len({r.chunk_id for r in approx} & gold) / len(gold)
        points.append(
            RecallPoint(
                ef_search=ef,
                recall_at_k=hits / len(vectors),
                mean_latency_ms=elapsed / len(vectors),
            )
        )
    return points


def format_recall_table(points: Sequence[RecallPoint]) -> str:
    """Plain-text recall curve, for the CLI and for pasting into the report."""
    lines = [f"{'ef_search':>10}  {'recall@k':>9}  {'ms':>8}  curve", "-" * 52]
    for p in points:
        bar = "#" * round(p.recall_at_k * 20)
        lines.append(f"{p.ef_search:>10}  {p.recall_at_k:>9.3f}  {p.mean_latency_ms:>8.1f}  {bar}")
    return "\n".join(lines)

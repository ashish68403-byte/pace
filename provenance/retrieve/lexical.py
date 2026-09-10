"""Lexical (BM25) retrieval over pg_search.

THE FAILURE THIS MODULE PREVENTS: "where is ``_normalise_url`` defined" returning
nothing useful because the identifier was shredded by the tokenizer.

Measured on this database (pg_search 0.25.7, PostgreSQL 18.6):

    SELECT 'let my_variable = 2;'::pdb.source_code::text[]
        -> {let,my,variable,2}
    SELECT '_normalise_url camelCaseName'::pdb.source_code::text[]
        -> {normalise,url,camel,case,name}
    SELECT 'def _normalise_url(u)'::pdb.literal::text[]
        -> {"def _normalise_url(u)"}

The WHOLE identifier does not survive `pdb.source_code`: it splits on
underscores and camel-case boundaries and throws the original away. Excellent
for recall ("normalise url" finds `_normalise_url`), useless for precision when
the user typed the exact symbol -- `_normalise_url`, `url_normalise` and
`normalise_url_cache` all become the same bag of tokens.

--------------------------------------------------------------------------
THE INDEX THAT ACTUALLY EXISTS (0001_core), AND WHY IT IS SHAPED THIS WAY
--------------------------------------------------------------------------
    CREATE INDEX chunks_bm25_idx ON chunks USING bm25 (
        chunk_row_id,
        (content::pdb.source_code),
        (qualified_name::pdb.literal),
        (lexical_blob::pdb.source_code)
    ) WITH (key_field = 'chunk_row_id');

Three constraints were hit for real while arriving at that, and each one closes
off an obvious alternative:

  1. The same column may NOT appear twice under two casts in one bm25 index
     -> ERROR: indexed attribute content defined more than once
     So "index content as both source_code and literal" is not available.
  2. A table may have only ONE ParadeDB index
     -> ERROR: a relation may only have one ParadeDB index
     So "one index per tokenizer" is not available either.
  3. pdb.literal emits the ENTIRE field as a single token, so casting a whole
     content column to literal would make each document one enormous term.
     literal is only useful on a SHORT field -- which qualified_name is.

Hence three DISTINCT columns, each with the tokenizer that suits its shape:

    content::pdb.source_code       split tokens        -> recall-oriented
    qualified_name::pdb.literal    the whole symbol    -> exact identifier match
    lexical_blob::pdb.source_code  augmented token bag -> the underscore-free
        joined form ('_normalise_url' -> 'normaliseurl') that source_code
        tokenization destroys and that a user typing a bare identifier produces.

Two consequences that are easy to get wrong and impossible to notice:

  * `paradedb.score()` must be passed the KEY FIELD, `chunk_row_id`. Passing
    chunk_id errors outright. chunk_row_id is a GENERATED IDENTITY surrogate and
    is NOT stable across a re-ingest, so every query joins it straight back to
    chunk_id and callers only ever see the content-addressed identity.
  * Because qualified_name is literal-tokenized, its single token is the ENTIRE
    dotted name. `_normalise_url` does NOT match
    `airflow.utils.helpers._normalise_url` on that field -- only the full string
    does. The leaf-name case is handled in router.lookup_symbol, not here.

The result lists are merged by max-score (see `_merge`), not summed: a chunk
that is a strong exact-symbol match should not be penalised for also being a
mediocre split-token match, and a chunk found by two fields should not get
double credit purely for the redundancy of indexing the same text twice.

Everything below is plain SQL through psycopg 3. No ORM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import psycopg

from provenance.config import settings
from provenance.graph.db import connection
from provenance.graph.repos import resolve_repo_id, split_repo_slug
from provenance.graph.tables import (
    BM25_FIELD_CONTENT,
    BM25_FIELD_LEXICAL,
    BM25_FIELD_QUALNAME,
    BM25_KEY_FIELD,
    CHUNKS,
    COL_CHUNK_ID,
    COL_LANGUAGE,
    COL_REPO_ID,
    COL_TOMBSTONE,
)

__all__ = [
    "LexicalForm",
    "LexicalResult",
    "lexical_search",
    "lexical_search_tsvector",
]

#: Which indexed field produced the winning score. "merged" means more than one
#: did, which is the strongest signal the lexical side can produce.
LexicalForm = Literal["content", "qualname", "lexical", "merged"]


@dataclass(frozen=True, slots=True)
class LexicalResult:
    chunk_id: str
    score: float
    rank: int  # 1-based; what fusion.py consumes
    form: LexicalForm


# ---------------------------------------------------------------------------
# BM25 via pg_search
# ---------------------------------------------------------------------------

# One statement per field. The score comes from the key_field, the identity
# comes from chunk_id, and the two are never confused: {field} is chosen from
# the three constants above, never from caller input.
_SQL_BM25 = """
SELECT c.{chunk_id}, paradedb.score(c.{key_field}) AS score
FROM {chunks} c
WHERE c.{field} @@@ %(query)s
  {filter_sql}
ORDER BY score DESC, c.{chunk_id}
LIMIT %(limit)s
"""

# Query-language metacharacters. The Tantivy-family parser behind pg_search
# reads `+ - ! ( ) : ^ [ ] { } ~ * ? \ / " |` as syntax, so a user pasting
# `TaskInstance.refresh_from_db(session=None)` or asking "why 429: retries?"
# would get a parse error instead of results. The source_code tokenizer would
# discard every one of these characters anyway, so blanking them costs nothing
# and removes an entire class of runtime failure.
_QUERY_META = re.compile(r"""[+\-!(){}\[\]:^~*?\\/"|&<>=,;#@$%]""")


def _sanitise(query: str) -> str:
    return " ".join(_QUERY_META.sub(" ", query).split())


def _resolve_repo(
    conn: psycopg.Connection,
    repo: str | None,
    repo_id: int | None,
) -> int | None:
    """`"apache/airflow"` -> bigint. chunks.repo_id is not the slug."""
    if repo_id is not None:
        return int(repo_id)
    if repo is None:
        return None
    owner, name = split_repo_slug(repo)
    return resolve_repo_id(conn, owner, name)


def _build_filter(
    *,
    repo_id: int | None,
    path_prefix: str | None,
    lang: str | None,
    include_tombstoned: bool,
) -> tuple[str, dict[str, object]]:
    clauses: list[str] = []
    params: dict[str, object] = {}
    if repo_id is not None:
        clauses.append(f"AND c.{COL_REPO_ID} = %(repo_id)s")
        params["repo_id"] = repo_id
    if path_prefix:
        clauses.append("AND c.path LIKE %(path_prefix)s")
        params["path_prefix"] = f"{path_prefix}%"
    if lang:
        clauses.append(f"AND c.{COL_LANGUAGE} = %(lang)s")
        params["lang"] = lang
    if not include_tombstoned:
        # Deleted code stays queryable ("why was this removed") but is not the
        # default retrieval surface; chunks_live_idx is partial on this exact
        # predicate.
        clauses.append(f"AND c.{COL_TOMBSTONE} IS NULL")
    return "\n  ".join(clauses), params


def _run(
    conn: psycopg.Connection,
    field: str,
    query: str,
    limit: int,
    filters: tuple[str, dict[str, object]],
) -> list[tuple[str, float]]:
    filter_sql, filter_params = filters
    sql = _SQL_BM25.format(
        chunk_id=COL_CHUNK_ID,
        key_field=BM25_KEY_FIELD,
        chunks=CHUNKS,
        field=field,
        filter_sql=filter_sql,
    )
    with conn.cursor() as cur:
        cur.execute(sql, {"query": query, "limit": limit, **filter_params})
        return [(row[0], float(row[1])) for row in cur.fetchall()]


def _merge(
    hits: dict[LexicalForm, list[tuple[str, float]]],
    boosts: dict[LexicalForm, float],
    limit: int,
) -> list[LexicalResult]:
    """Max-merge the per-field lists, with a per-field boost.

    Scores from the three fields are on the same scale (BM25 over the same
    corpus, same index), so a max is meaningful here. This is the ONE place
    scores are compared directly; across lexical-vs-dense they are not
    comparable at all, which is why fusion.py is rank-based. See its docstring.
    """
    best: dict[str, tuple[float, LexicalForm]] = {}
    seen: dict[str, int] = {}
    for form, rows in hits.items():
        boost = boosts.get(form, 1.0)
        for cid, score in rows:
            seen[cid] = seen.get(cid, 0) + 1
            scaled = score * boost
            current = best.get(cid)
            if current is None or scaled > current[0]:
                best[cid] = (scaled, form)

    ordered = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))[:limit]
    return [
        LexicalResult(
            chunk_id=cid,
            score=score,
            rank=i,
            form="merged" if seen[cid] > 1 else form,
        )
        for i, (cid, (score, form)) in enumerate(ordered, start=1)
    ]


def lexical_search(
    query: str,
    limit: int = 50,
    *,
    repo: str | None = None,
    repo_id: int | None = None,
    path_prefix: str | None = None,
    lang: str | None = None,
    include_tombstoned: bool = False,
    qualname_boost: float = 1.25,
) -> list[LexicalResult]:
    """BM25 over the three indexed fields, merged.

    ``qualname_boost`` is a thumb on the scale for an exact whole-symbol match.
    1.25 is a starting point, tuned on the golden set alongside the RRF
    constant -- not a constant with any theory behind it. Set it to 1.0 to
    disable.

    ``repo`` takes the ``owner/name`` slug and is resolved to the bigint
    ``repo_id`` here; pass ``repo_id`` directly if you already have it.

    Ordering is deterministic: ties break on chunk_id, so the same query against
    the same snapshot yields byte-identical output. The ingest gate depends on
    that (T5).
    """
    q = _sanitise(query)
    if not q:
        return []

    # Over-fetch each field: the merge is a union, and taking `limit` from each
    # side before merging would let a chunk that ranks 60th on content and 1st
    # on qualified_name fall out of the union entirely.
    fetch = limit * 2

    with connection() as conn:
        rid = _resolve_repo(conn, repo, repo_id)
        filters = _build_filter(
            repo_id=rid,
            path_prefix=path_prefix,
            lang=lang,
            include_tombstoned=include_tombstoned,
        )
        hits: dict[LexicalForm, list[tuple[str, float]]] = {
            "content": _run(conn, BM25_FIELD_CONTENT, q, fetch, filters),
            "lexical": _run(conn, BM25_FIELD_LEXICAL, q, fetch, filters),
        }
        # qualified_name is literal-tokenized: its single token is the whole
        # dotted name, which never contains whitespace. A multi-word query
        # therefore cannot match it, and issuing the statement would burn a
        # round trip to prove it. Quoted so the parser treats the dots as part
        # of one term rather than as syntax.
        if " " not in q:
            hits["qualname"] = _run(conn, BM25_FIELD_QUALNAME, f'"{q}"', fetch, filters)

    return _merge(hits, {"qualname": qualname_boost}, limit)


# ---------------------------------------------------------------------------
# Fallback: no pg_search
# ---------------------------------------------------------------------------

_TERM_RE = re.compile(r"[A-Za-z0-9]+")

# Candidate generation is the array-overlap operator, which IS what
# chunks_lexical_tokens_idx (a GIN index on the text[] column) accelerates.
# `array_to_tsvector(lexical_tokens) @@ q` cannot use that index at all -- it is
# a different operator class on a different expression -- so it is used for
# RANKING only, over rows the GIN index already narrowed.
_SQL_TSVECTOR = f"""
SELECT c.{COL_CHUNK_ID},
       ts_rank_cd(array_to_tsvector(c.lexical_tokens), q.tsq) AS score
FROM {CHUNKS} c, to_tsquery('simple', %(tsquery)s) AS q(tsq)
WHERE c.lexical_tokens && %(terms)s::text[]
  {{filter_sql}}
ORDER BY score DESC, c.{COL_CHUNK_ID}
LIMIT %(limit)s
"""


def lexical_search_tsvector(
    query: str,
    limit: int = 50,
    *,
    repo: str | None = None,
    repo_id: int | None = None,
    path_prefix: str | None = None,
    lang: str | None = None,
    include_tombstoned: bool = False,
) -> list[LexicalResult]:
    """Fallback lexical search on stock PostgreSQL, over ``lexical_tokens``.

    THIS IS NOT BM25. Say it plainly, because it will be asked:

      * ts_rank_cd has NO inverse document frequency. A match on "the" and a
        match on "kubernetes_executor" contribute the same weight. BM25's single
        most important term is the one this ranker does not have.
      * It has NO document length normalisation. BM25's b parameter interpolates
        between "long documents are naturally term-rich, discount them" and "do
        not". ts_rank_cd divides by a document-length figure only if you pass a
        normalisation bitmask, and even then it is not BM25's saturation curve.
      * It has NO k1 term-frequency saturation. Term frequency contributes
        roughly linearly, so a chunk repeating a token twenty times outranks a
        chunk that uses it meaningfully twice.
      * ts_rank_cd's "cd" is cover density -- proximity of matched terms. That
        is a different ranking signal, not a weaker BM25.

    So this path exists to keep the system RUNNABLE on a plain postgres (a
    marker's laptop, a container without pg_search), not to be an equivalent
    ranker. Any number measured on this path must be labelled as such; comparing
    a tsvector run against a pg_search run and calling the delta "the effect of
    hybrid retrieval" would be measuring the ranker, not the architecture.

    ``lexical_tokens`` is written by the chunker and already contains the split
    parts and the underscore-free joined forms, so the exact-identifier problem
    is softened here by the token array's contents rather than by a second
    index. The query is reduced to the same alphanumeric-run vocabulary the
    chunker produced (``[A-Za-z0-9]+``, lowercased) so the two sides cannot
    disagree about how an underscore tokenizes -- which is exactly what a
    default text-search parser would do differently.
    """
    terms = sorted({t.lower() for t in _TERM_RE.findall(query)})
    if not terms:
        return []
    # terms are [A-Za-z0-9]+ by construction, so the OR-query handed to
    # to_tsquery() cannot carry operator syntax.
    tsquery = " | ".join(terms)

    with connection() as conn, conn.cursor() as cur:
        rid = _resolve_repo(conn, repo, repo_id)
        filter_sql, filter_params = _build_filter(
            repo_id=rid,
            path_prefix=path_prefix,
            lang=lang,
            include_tombstoned=include_tombstoned,
        )
        cur.execute(
            _SQL_TSVECTOR.format(filter_sql=filter_sql),
            {"tsquery": tsquery, "terms": terms, "limit": limit, **filter_params},
        )
        rows = cur.fetchall()
    return [
        LexicalResult(chunk_id=r[0], score=float(r[1]), rank=i, form="lexical")
        for i, r in enumerate(rows, start=1)
    ]


def default_repo_id(conn: psycopg.Connection) -> int:
    """The configured corpus as a bigint. Convenience for callers holding a conn."""
    return resolve_repo_id(conn, settings.repo_owner, settings.repo_name)

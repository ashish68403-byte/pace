"""Query routing: pick the cheapest retrieval strategy that can answer the query.

THE FAILURE THIS MODULE PREVENTS: burning an embedding call, an HNSW walk and a
fusion pass to answer "where is DagRun defined", and getting a worse answer than
an index lookup would have given.

"Where is X defined" is a lookup, not a search. `chunks.qualified_name` is in the
one bm25 index under `pdb.literal`, whose token is the WHOLE dotted symbol name
-- so an exact identifier match is a single indexed term lookup. Answering from
that is cheaper (no embedding, no vector scan), faster, and MORE ACCURATE: an
exact match on a symbol name cannot be beaten by an approximate nearest
neighbour that merely looks similar. Routing here is not an optimisation, it is
a correctness improvement on the class of queries where an exact answer exists.

The literal field is load bearing. `content::pdb.source_code` shreds
`_normalise_url` into {normalise, url}; the btree `chunks_qname_idx` is
`(repo_id, path, qualified_name)`, so its leading columns are wrong for a
symbol-only lookup and it cannot serve one. The literal-tokenized
`qualified_name` is the ONLY structure in the schema that makes exact identifier
match an index lookup rather than a scan. If a future migration drops the
literal cast, this whole route silently degrades to a sequential scan that still
returns the right rows -- correct, and ten to a hundred times slower.

The rules are hand-written regexes with a fallthrough to hybrid. Deliberately no
ML classifier:

  * The rules are EXPLAINABLE. When a query routes wrongly, the trace shows
    which rule fired and why, and the fix is a one-line edit that can be
    unit-tested. A classifier's misroute is a probability, and the fix is to
    retrain on data that does not exist for a solo 14-week project.
  * A classifier needs labelled queries. Labelling them means inventing them,
    and invented queries are exactly the leakage problem the golden set already
    fights (grep recall@10 = 0.986 on a naive set).
  * The fallthrough is safe. Hybrid answers everything, just less cheaply, so a
    missed rule costs latency, never an answer. That asymmetry is what makes a
    small rule set defensible; it would not be if a wrong route could return
    nothing.
  * A router is not where this project's novelty is. Provenance is.

Every route records which rule fired, so the eval can report the routing
distribution and a misroute is a visible, attributable event rather than a
mystery in the aggregate.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import psycopg

from provenance.graph.db import connection
from provenance.graph.repos import resolve_repo_id, split_repo_slug
from provenance.graph.tables import (
    BM25_FIELD_QUALNAME,
    CHUNKS,
    COL_CHUNK_ID,
    COL_REPO_ID,
    COL_TOMBSTONE,
)

__all__ = [
    "Route",
    "RouteKind",
    "SymbolHit",
    "explain_route",
    "lookup_symbol",
    "route_query",
    "routing_distribution",
]


class RouteKind(StrEnum):
    SYMBOL = "symbol"  # exact qualified_name / definition lookup
    LEXICAL = "lexical"  # identifier-ish or quoted: BM25 alone is enough
    HYBRID = "hybrid"  # everything else: BM25 + dense + RRF


@dataclass(frozen=True, slots=True)
class Route:
    kind: RouteKind
    rule: str  # which rule fired; goes into the trace
    query: str  # possibly rewritten (e.g. stripped of "where is ... defined")
    symbol: str | None = None
    lexical_weight: float = 1.0
    dense_weight: float = 1.0


# --- rule patterns ---------------------------------------------------------
# Ordered; first match wins. Each captures the symbol in group "sym" where it has one.

_DEFINITION = re.compile(
    r"""^\s*
    (?:where(?:'s|\s+is|\s+are)?|find|show\s+me|locate|goto|go\s+to|jump\s+to)?
    \s*
    (?:the\s+)?
    (?:definition|declaration|implementation|source|body)?
    \s*(?:of|for)?\s*
    (?P<sym>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
    \s*
    (?:\(\s*\))?
    \s*
    (?:is\s+)?(?:defined|declared|implemented)?
    \s*[?.]?\s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

# A bare identifier typed alone: `_normalise_url`, `TaskInstance.refresh_from_db`.
_BARE_SYMBOL = re.compile(
    r"^\s*(?P<sym>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*(?:\(\s*\))?\s*$"
)

# Must contain a snake_case / camelCase / dotted token, or a "quoted string",
# or an ALL_CAPS constant -- signals the user knows the literal text they want.
_IDENTIFIER_TOKEN = re.compile(
    r"""(?:"[^"]{2,}"|'[^']{2,}'
        |\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b
        |\b[a-z]+[A-Z][A-Za-z0-9]*\b
        |\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)*\b
        |\b\w+\.\w+\b)""",
    re.VERBOSE,
)

# "why", "rationale", "reason" queries are the project's whole point and are
# never identifier lookups, however many identifiers they happen to mention.
_RATIONALE = re.compile(
    r"\b(why|rationale|reason(?:ing)?|motivation|because|trade[- ]?off|"
    r"decided|decision|justif\w+|history|discuss\w*)\b",
    re.IGNORECASE,
)

# Words that make a "where is X" query a semantic question, not a lookup:
# "where is retry handled", "where do we validate the schema".
_NOT_A_SYMBOL = frozenset(
    {
        "this",
        "that",
        "it",
        "code",
        "logic",
        "thing",
        "stuff",
        "handled",
        "done",
        "used",
        "called",
        "set",
        "configured",
        "here",
        "everything",
    }
)


def route_query(query: str) -> Route:
    """Classify a query into a retrieval strategy. Never raises; worst case HYBRID."""
    q = query.strip()
    if not q:
        return Route(RouteKind.HYBRID, rule="empty", query=q)

    # Rationale beats everything. "why does _normalise_url strip the port" looks
    # identifier-shaped to every rule below and is emphatically not a lookup --
    # the answer lives in a commit message, not in the function body. Ordering
    # this first is the single most important line in the file for THIS project.
    if _RATIONALE.search(q):
        return Route(
            RouteKind.HYBRID,
            rule="rationale",
            query=q,
            # Rationale text is prose in commits and reviews; lean dense, but do
            # not switch lexical off -- the identifier still anchors the answer.
            lexical_weight=0.8,
            dense_weight=1.2,
        )

    m = _DEFINITION.match(q)
    if m and m.group("sym") and m.group("sym").lower() not in _NOT_A_SYMBOL:
        sym = m.group("sym")
        # A single lowercase English word ("where is retry defined") is more
        # likely a concept than a symbol; require an identifier shape.
        if "_" in sym or "." in sym or not sym.islower() or len(sym) > 12:
            return Route(RouteKind.SYMBOL, rule="definition", query=q, symbol=sym)

    m = _BARE_SYMBOL.match(q)
    if m:
        sym = m.group("sym")
        if ("_" in sym or "." in sym or not sym.islower()) and sym.lower() not in _NOT_A_SYMBOL:
            return Route(RouteKind.SYMBOL, rule="bare-symbol", query=q, symbol=sym)

    if _IDENTIFIER_TOKEN.search(q) and len(q.split()) <= 4:
        # Short and full of literal tokens: the embedding of four identifiers is
        # mush, BM25 over the literal form is not. Still returns to the caller
        # as a rank list, so fusion can be applied if the caller wants both.
        return Route(RouteKind.LEXICAL, rule="short-identifier", query=q, lexical_weight=1.3)

    return Route(RouteKind.HYBRID, rule="fallthrough", query=q)


# ---------------------------------------------------------------------------
# Symbol lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SymbolHit:
    chunk_id: str
    path: str
    qualified_name: str
    rank: int
    start_line: int = 0
    end_line: int = 0
    #: False when the chunk looks like a typing stub (an `@overload` signature
    #: with no implementation). Kept on the result, not just used for ordering,
    #: so a caller citing a definition can say which one it cited.
    has_body: bool = True


# A qualified_name is NOT unique within a file. Measured on the corpus: THREE
# chunks are named `is_container` in airflow/utils/helpers.py -- two @overload
# stubs and the implementation. Returning a stub as "the definition" is a wrong
# answer that looks completely right, because the name, path and language all
# match. The decorator survives into chunk content (parse/chunker.py keeps it
# deliberately: `@provide_session` is often the whole answer to "why does this
# take a session"), so it is detectable.
#
# Postgres AREs are not PCRE: \b is BACKSPACE here, \y is the word boundary.
# Getting that wrong makes this predicate match nothing and every stub sorts
# level with the implementation again, silently.
_STUB_RE = r"(^|\n)[ \t]*@(typing\.)?overload\y"

_SQL_SYMBOL = """
SELECT c.{chunk_id}, c.path, c.qualified_name, c.start_line, c.end_line,
       (c.content ~ %(stub_re)s) AS is_stub
FROM {chunks} c
WHERE c.qualified_name IS NOT NULL
  AND {match_sql}
  {filter_sql}
ORDER BY
  -- Prefer a real implementation over an @overload stub, then the longest body
  -- (a stub is one or two lines), then the least-nested name, then a stable id
  -- tiebreak so the ordering is deterministic for the ingest gate.
  (c.content ~ %(stub_re)s) ASC,
  (c.end_line - c.start_line) DESC,
  length(c.qualified_name) ASC,
  c.{chunk_id} ASC
LIMIT %(limit)s
"""


def _resolve_repo(
    conn: psycopg.Connection,
    repo: str | None,
    repo_id: int | None,
) -> int | None:
    if repo_id is not None:
        return int(repo_id)
    if repo is None:
        return None
    owner, name = split_repo_slug(repo)
    return resolve_repo_id(conn, owner, name)


def _filter_sql(repo_id: int | None, include_tombstoned: bool) -> str:
    clauses: list[str] = []
    if repo_id is not None:
        clauses.append(f"AND c.{COL_REPO_ID} = %(repo_id)s")
    if not include_tombstoned:
        clauses.append(f"AND c.{COL_TOMBSTONE} IS NULL")
    return "\n  ".join(clauses)


def _rows_to_hits(rows: Sequence[tuple], start_rank: int) -> list[SymbolHit]:
    return [
        SymbolHit(
            chunk_id=r[0],
            path=r[1],
            qualified_name=r[2],
            rank=i,
            start_line=int(r[3]),
            end_line=int(r[4]),
            has_body=not bool(r[5]),
        )
        for i, r in enumerate(rows, start=start_rank)
    ]


def lookup_symbol(
    symbol: str,
    limit: int = 10,
    *,
    repo: str | None = None,
    repo_id: int | None = None,
    include_tombstoned: bool = False,
) -> list[SymbolHit]:
    """Exact symbol lookup on qualified_name. No embedding, no vector scan.

    Two stages, in order of both precision and cost:

      1. EXACT, INDEXED. `qualified_name @@@ '"sym"'` against the literal-
         tokenized field of chunks_bm25_idx. pdb.literal emits the entire field
         as one token, so this matches the whole dotted name and nothing else --
         no prefix, no substring, no tokenizer surprises.
      2. LEAF NAME, SCANNED. Only if stage 1 did not fill `limit` and the user
         typed an undotted leaf (`refresh_from_db` for
         `TaskInstance.refresh_from_db`). A literal token cannot be matched by
         its suffix, so this stage is a filtered scan over the repo's live
         chunks. It is bounded by the scoped corpus (~60-80k lines) and it is
         deliberately second, not merged: a full-name match must never be
         outranked by a leaf-name coincidence.

    Stage 2 matches with `right(qualified_name, n) = '.sym'`, NOT with
    `LIKE '%.sym'`. In LIKE, `_` is a single-character wildcard, so the pattern
    `%.refresh_from_db` also matches `x.refreshXfromYdb` -- which is a
    grep-shaped substring match sneaking into the one path that exists to be
    exact, and precisely what the leakage probe is there to keep out of the
    benchmark.

    Results prefer a real implementation over an `@overload` stub; see _STUB_RE.
    """
    sym = symbol.strip()
    if not sym:
        return []

    with connection() as conn, conn.cursor() as cur:
        rid = _resolve_repo(conn, repo, repo_id)
        filter_sql = _filter_sql(rid, include_tombstoned)
        params: dict[str, object] = {
            "sym": sym,
            # The bm25 term for a literal field is the entire field value.
            # Quoted so the query parser reads the dots as part of one term.
            "bm25_sym": f'"{sym}"',
            "stub_re": _STUB_RE,
            "limit": limit,
        }
        if rid is not None:
            params["repo_id"] = rid

        # The `= %(sym)s` next to the `@@@` is not redundant belt-and-braces: it
        # is what keeps this path exact if a future migration ever changes the
        # tokenizer on qualified_name. The @@@ is what makes it an index lookup.
        indexed_match = f"c.{BM25_FIELD_QUALNAME} @@@ %(bm25_sym)s AND c.qualified_name = %(sym)s"
        try:
            cur.execute(
                _SQL_SYMBOL.format(
                    chunk_id=COL_CHUNK_ID,
                    chunks=CHUNKS,
                    match_sql=indexed_match,
                    filter_sql=filter_sql,
                ),
                params,
            )
            exact = cur.fetchall()
        except psycopg.errors.UndefinedFunction:
            # No pg_search on this server (a plain postgres:18 image, a marker's
            # laptop). Same fallback contract as lexical_search_tsvector: keep
            # the system RUNNABLE and equally correct, just unindexed. The
            # failed statement aborted the transaction, so roll back first.
            conn.rollback()
            cur.execute(
                _SQL_SYMBOL.format(
                    chunk_id=COL_CHUNK_ID,
                    chunks=CHUNKS,
                    match_sql="c.qualified_name = %(sym)s",
                    filter_sql=filter_sql,
                ),
                params,
            )
            exact = cur.fetchall()

        suffix: list[tuple] = []
        if len(exact) < limit and "." not in sym:
            params["dotsym"] = f".{sym}"
            params["dotsym_len"] = len(sym) + 1
            params["limit"] = limit - len(exact)
            cur.execute(
                _SQL_SYMBOL.format(
                    chunk_id=COL_CHUNK_ID,
                    chunks=CHUNKS,
                    match_sql="right(c.qualified_name, %(dotsym_len)s) = %(dotsym)s",
                    filter_sql=filter_sql,
                ),
                params,
            )
            suffix = cur.fetchall()

    hits = _rows_to_hits(exact, 1)
    return hits + _rows_to_hits(suffix, len(hits) + 1)


def explain_route(route: Route) -> str:
    """One line for the trace and the CLI. Routing must be legible after the fact."""
    target = f" symbol={route.symbol!r}" if route.symbol else ""
    return (
        f"route={route.kind.value} rule={route.rule}{target} "
        f"w_lex={route.lexical_weight} w_dense={route.dense_weight}"
    )


def routing_distribution(queries: Sequence[str]) -> dict[str, int]:
    """Count which rules fire across a query set.

    Run it over the golden set before trusting the router: a rule that fires on
    60% of queries is doing something you did not intend, and a rule that never
    fires is dead code claiming to be a design decision.
    """
    counts: dict[str, int] = {}
    for q in queries:
        r = route_query(q)
        key = f"{r.kind.value}:{r.rule}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

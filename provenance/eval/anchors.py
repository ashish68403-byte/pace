"""Resolve stable gold anchors to current chunk ids, at scoring time.

What this module is for: it is the join between a golden set that never changes
and an index that changes every week.

The failure it prevents: a golden set pinned to ``chunk_id`` values. Chunk ids are
content hashes (``provenance/graph/ids.py``); re-tuning the chunker rewrites all of
them, and a set pinned to them would report a catastrophic recall drop that is
purely an artefact of re-hashing. Resolving anchors here means a chunker change
costs one re-scoring run instead of a re-labelling week.

Two contracts this module depends on. Both are physical schema, so every name
comes from ``provenance.graph.tables`` and none is typed as a literal here -- a
name that drifts is a runtime `relation does not exist`, discoverable only by
executing the query:

    chunks(chunk_id sha256_hex PK, repo_id bigint, path text,
           qualified_name text NULL, tombstoned_at_commit git_sha NULL)
    chunk_evidence(repo_id bigint, chunk_id sha256_hex, evidence_kind artifact_kind,
                   evidence_key text, source text, confidence real)
        -- migration 0005, lookup index on (repo_id, evidence_kind, evidence_key)

``repo_id`` is a **bigint**, never the ``"apache/airflow"`` slug. Callers resolve
it once with ``provenance.graph.repos.resolve_repo_id``; passing the slug raises
`invalid input syntax for type bigint`, but only after a fixture load and a
scoring loop have already run.

Tombstones matter. Archaeology questions ("why was this removed?") have gold
evidence that is, by construction, not in HEAD. If the resolver filtered
tombstoned chunks for those queries the gold would resolve to nothing and the
system would be scored 0.0 for correctly retrieving deleted code.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from provenance.eval.schema import EvidenceKey, GoldenRecord, QueryClass, SymbolAnchor
from provenance.graph.tables import (
    CHUNK_EVIDENCE,
    CHUNKS,
    COL_CHUNK_ID,
    COL_REPO_ID,
    COL_TOMBSTONE,
)

# No psycopg import here on purpose: the metrics/schema tests, and `pace eval --help`,
# must import this package on a machine with no database driver configured.

#: Golden-set evidence kind -> the `artifact_kind` enum label stored in
#: chunk_evidence.evidence_kind (0001_core: commit | file | file_revision | chunk |
#: symbol | pull_request | issue | issue_comment | review_comment).
#: The golden set says "pr"; the enum says "pull_request". Sending "pr" straight
#: through is `invalid input value for enum artifact_kind: "pr"` -- and, if the
#: parameter were ever cast to text instead, a silent zero-row join, which is
#: worse: it scores as a retrieval miss.
GOLD_KIND_TO_ARTIFACT: dict[str, str] = {
    "commit": "commit",
    "pr": "pull_request",
    "issue": "issue",
    "review_comment": "review_comment",
}
ARTIFACT_TO_GOLD_KIND: dict[str, str] = {v: k for k, v in GOLD_KIND_TO_ARTIFACT.items()}

#: Evidence kinds this module resolves through chunk_evidence. ("symbol" keys are
#: resolved against chunks directly and are deliberately not in here.)
EVIDENCE_KINDS: tuple[str, ...] = tuple(GOLD_KIND_TO_ARTIFACT)

#: Full git object ids are 40 hex chars. Anything shorter in the golden set is an
#: abbreviation read off `git log` during curation and is matched on prefix.
FULL_SHA_LEN = 40


class SupportsQuery(Protocol):
    """Just enough of psycopg's Connection to keep this testable with a fake."""

    def execute(self, query: str, params: Any = None) -> Any: ...


@dataclass(slots=True)
class ResolvedAnchors:
    """The scoring-time view of one record's gold evidence."""

    qid: str
    required_chunk_ids: set[str] = field(default_factory=set)
    supporting_chunk_ids: set[str] = field(default_factory=set)
    #: Anchors that matched no chunk. A non-empty set here is a *scoring* bug or an
    #: ingest gap -- never a retrieval miss -- so the runner surfaces it separately
    #: instead of quietly counting it against the system.
    unresolved_required: set[EvidenceKey] = field(default_factory=set)
    unresolved_supporting: set[EvidenceKey] = field(default_factory=set)
    included_tombstoned: bool = False

    @property
    def gains(self) -> dict[str, float]:
        """Graded relevance for nDCG: required outranks supporting.

        A chunk that is both required and supporting keeps the required gain.
        """
        from provenance.eval.metrics import REQUIRED_GAIN, SUPPORTING_GAIN

        g: dict[str, float] = {cid: SUPPORTING_GAIN for cid in self.supporting_chunk_ids}
        g.update({cid: REQUIRED_GAIN for cid in self.required_chunk_ids})
        return g

    @property
    def n_unresolved(self) -> int:
        return len(self.unresolved_required) + len(self.unresolved_supporting)


def _resolve_symbols(
    conn: SupportsQuery,
    repo_id: int,
    anchors: Sequence[SymbolAnchor],
    *,
    include_tombstoned: bool,
) -> dict[EvidenceKey, set[str]]:
    """(path, qualified_name) -> chunk ids, in one round trip.

    LEFT JOIN, with the tombstone predicate in the ON clause: an anchor that
    resolves to nothing must come back as a row with a NULL chunk_id so the caller
    can tell "unresolved" from "not asked for".
    """
    if not anchors:
        return {}
    paths = [a.path for a in anchors]
    # SQL equality is not NULL-safe, so file-level anchors (qualified_name is None)
    # travel as the empty string and are matched with coalesce on the column side.
    qnames = [a.qualified_name or "" for a in anchors]
    sql = f"""
        SELECT a.path, a.qualified_name, c.{COL_CHUNK_ID}
        FROM unnest(%(paths)s::text[], %(qnames)s::text[]) WITH ORDINALITY
             AS a(path, qualified_name, ord)
        LEFT JOIN {CHUNKS} c
               ON c.{COL_REPO_ID} = %(repo_id)s
              AND c.path = a.path
              AND coalesce(c.qualified_name, '') = a.qualified_name
              AND (%(include_tombstoned)s OR c.{COL_TOMBSTONE} IS NULL)
        ORDER BY a.ord
    """
    out: dict[EvidenceKey, set[str]] = {a.key: set() for a in anchors}
    cur = conn.execute(
        sql,
        {
            "paths": paths,
            "qnames": qnames,
            "repo_id": repo_id,
            "include_tombstoned": include_tombstoned,
        },
    )
    # Rows are TUPLES (graph.db.connection defaults to tuple_row); positional
    # unpacking is correct by default and must not be "fixed" to dict access.
    for path, qname, chunk_id in cur.fetchall():
        if chunk_id is None:
            continue
        out[("symbol", f"{path}::{qname}")].add(chunk_id)
    return out


def _resolve_evidence(
    conn: SupportsQuery,
    repo_id: int,
    keys: Sequence[EvidenceKey],
    *,
    include_tombstoned: bool,
) -> dict[EvidenceKey, set[str]]:
    """commit/pr/issue/review_comment keys -> chunk ids, via ``chunk_evidence``.

    THIS FUNCTION IS THE MECHANISM THAT LETS THE GOLDEN SET SURVIVE A RE-CHUNK.
    Gold is keyed on identifiers git and GitHub own -- a commit sha, a PR number,
    an issue number, a review-comment id -- precisely because those do not move
    when the chunker is re-tuned. Scoring, however, compares against retrieved
    chunk ids, which are content hashes and therefore change with every chunker
    experiment. ``chunk_evidence`` (migration 0005) is the derived edge table that
    carries "PR 47798 is evidence for these chunks", rebuilt by the ingester on
    every re-chunk; this query is the only read of it on the scoring path. If it
    returns nothing, every answerable query scores 0.0 and the drop looks like a
    retrieval regression rather than a broken join -- which is why an empty result
    is reported as an *unresolved anchor* (exit 2) and never as a miss.

    Two details that are easy to regress:

    * ``evidence_kind`` is the ``artifact_kind`` enum, so the golden "pr" is
      translated to "pull_request" and the *parameter* is cast to the enum. Cast
      the column instead (``l.evidence_kind::text = ...``) and the query still
      returns the right rows while silently abandoning
      ``chunk_evidence_lookup_idx`` -- correct answers, sequential scan, no test
      catches it.
    * ``evidence_key`` is TEXT for every kind -- a 40-hex sha, a PR number, an
      issue number, a review-comment id -- so both sides are compared as text and
      numbers are never allowed to arrive as ints. Full shas and all non-commit
      kinds use plain equality (index-usable); only an abbreviated sha falls back
      to a prefix scan, because curation reads short shas off ``git log`` while
      ingest stores all 40 characters.
    """
    wanted = [k for k in keys if k[0] in GOLD_KIND_TO_ARTIFACT]
    if not wanted:
        return {}
    kinds = [GOLD_KIND_TO_ARTIFACT[k] for k, _ in wanted]
    values = [str(v) for _, v in wanted]
    sql = f"""
        SELECT e.kind, e.ekey, c.{COL_CHUNK_ID}
        FROM unnest(%(kinds)s::text[], %(keys)s::text[]) WITH ORDINALITY
             AS e(kind, ekey, ord)
        LEFT JOIN {CHUNK_EVIDENCE} l
               ON l.{COL_REPO_ID} = %(repo_id)s
              AND l.evidence_kind = e.kind::artifact_kind
              AND (CASE WHEN e.kind = 'commit' AND length(e.ekey) < {FULL_SHA_LEN}
                        THEN starts_with(l.evidence_key, e.ekey)
                        ELSE l.evidence_key = e.ekey END)
        LEFT JOIN {CHUNKS} c
               ON c.{COL_CHUNK_ID} = l.{COL_CHUNK_ID}
              AND c.{COL_REPO_ID} = %(repo_id)s
              AND (%(include_tombstoned)s OR c.{COL_TOMBSTONE} IS NULL)
        ORDER BY e.ord
    """
    out: dict[EvidenceKey, set[str]] = {k: set() for k in wanted}
    cur = conn.execute(
        sql,
        {
            "kinds": kinds,
            "keys": values,
            "repo_id": repo_id,
            "include_tombstoned": include_tombstoned,
        },
    )
    for kind, key, chunk_id in cur.fetchall():
        if chunk_id is None:
            continue
        # Back to the golden-set spelling: the enum said "pull_request", the
        # record said "pr", and the caller looks the result up by the record's key.
        out[(ARTIFACT_TO_GOLD_KIND.get(kind, kind), key)].add(chunk_id)
    return out


def resolve_anchors(
    record: GoldenRecord,
    conn: SupportsQuery,
    *,
    repo_id: int,
    include_tombstoned: bool | None = None,
) -> ResolvedAnchors:
    """Map one record's stable anchors onto the chunk ids currently in the index.

    ``include_tombstoned`` defaults to True for archaeology queries and False
    otherwise: the other classes ask about code that exists, and admitting
    tombstones there would let a system score by retrieving a deleted ancestor of
    the right function.
    """
    if include_tombstoned is None:
        include_tombstoned = record.query_class is QueryClass.ARCHAEOLOGY

    resolved = ResolvedAnchors(qid=record.qid, included_tombstoned=include_tombstoned)

    for tier, evidence in (
        ("required", record.required_evidence),
        ("supporting", record.supporting_evidence),
    ):
        keys = evidence.keys()
        if not keys:
            continue
        mapping = _resolve_symbols(
            conn, repo_id, evidence.symbol_anchors, include_tombstoned=include_tombstoned
        )
        mapping.update(
            _resolve_evidence(conn, repo_id, keys, include_tombstoned=include_tombstoned)
        )
        chunk_ids: set[str] = set()
        unresolved: set[EvidenceKey] = set()
        for key in keys:
            hits = mapping.get(key, set())
            if hits:
                chunk_ids |= hits
            else:
                unresolved.add(key)
        if tier == "required":
            resolved.required_chunk_ids = chunk_ids
            resolved.unresolved_required = unresolved
        else:
            resolved.supporting_chunk_ids = chunk_ids
            resolved.unresolved_supporting = unresolved

    # A chunk cannot occupy both tiers; required wins so recall and nDCG agree.
    resolved.supporting_chunk_ids -= resolved.required_chunk_ids
    return resolved


def resolve_all(
    records: Iterable[GoldenRecord],
    conn: SupportsQuery,
    *,
    repo_id: int,
) -> dict[str, ResolvedAnchors]:
    return {r.qid: resolve_anchors(r, conn, repo_id=repo_id) for r in records}


def index_is_populated(conn: SupportsQuery, repo_id: int) -> bool:
    """True when there is anything to resolve against.

    Week 1 has an empty ``chunks`` table. The runner uses this to report clean
    zeros and exit 0 rather than failing a gate against an index that does not
    exist yet.
    """
    row = conn.execute(
        f"SELECT EXISTS (SELECT 1 FROM {CHUNKS} WHERE {COL_REPO_ID} = %s)", (repo_id,)
    ).fetchone()
    return bool(row and row[0])

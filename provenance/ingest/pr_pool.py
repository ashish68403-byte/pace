"""Slice B4: the pool's PR text as documents (ADR-0003 §1-§2, second 2026-10-29 addendum).

`pace pool build` writes these after the commit messages, one document per artifact:

    PR description         kind pull_request     path pull/<n>
    inline review comment  kind review_comment   path pull/<n>/review/<id>
    conversation comment   kind issue_comment    path pull/<n>/comment/<id>

A description is its title, a blank line, and its body with HTML comments removed: they hold
the PR template's instructions, which GitHub never displays. Bot-written text and empty
comments never become documents. Every document is `self` evidence for its own artifact and
`linked` evidence for its PR and for the commit B2 linked to that PR, so any document of a
change satisfies both of a record's relevance units (ADR-0003 §4).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from provenance.graph.ids import chunk_id, normalize_content
from provenance.graph.tables import (
    CHUNK_EVIDENCE,
    CHUNKS,
    ISSUE_COMMENTS,
    ISSUES,
    PULL_REQUESTS,
    REPOSITORIES,
    REVIEW_COMMENTS,
)
from provenance.ingest.message_pool import PoolInvariantError
from provenance.parse.chunker import lexical_tokens

PR_STRATEGY_VERSION: Final = "pull-request-v1"
PR_KINDS: Final = ("pull_request", "review_comment", "issue_comment")
_HTML_COMMENT_RE: Final = re.compile(r"<!--.*?-->", re.DOTALL)

#: (evidence_kind, evidence_key, source)
Evidence = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class Artifact:
    """One stored PR text, as the pool build reads it."""

    kind: str
    pr: int
    artifact_id: int | None  # None for a description
    text: str
    author_is_bot: bool


@dataclass(frozen=True, slots=True)
class PrDocument:
    """The `chunks` row and evidence for one PR artifact."""

    chunk_id: str
    kind: str
    path: str
    content: str
    content_sha: str
    end_line: int
    lexical_tokens: tuple[str, ...]
    commit_sha: str
    evidence: tuple[Evidence, ...]

    @property
    def lexical_blob(self) -> str:
        return " ".join(self.lexical_tokens)


def description_text(title: str, body: str) -> str:
    """A PR description document's text, normalised."""
    return normalize_content(f"{title}\n\n{_HTML_COMMENT_RE.sub('', body)}")


def pr_document(artifact: Artifact, *, commit_sha: str, repo_slug: str) -> PrDocument | None:
    """The document for one artifact, or None for bot-written or empty text."""
    if artifact.author_is_bot:
        return None
    content = normalize_content(artifact.text)
    if not content.strip():
        return None
    if artifact.kind == "pull_request":
        path, own = f"pull/{artifact.pr}", str(artifact.pr)
    else:
        segment = "review" if artifact.kind == "review_comment" else "comment"
        path, own = (
            f"pull/{artifact.pr}/{segment}/{artifact.artifact_id}",
            str(artifact.artifact_id),
        )
    evidence: list[Evidence] = [(artifact.kind, own, "self")]
    if artifact.kind != "pull_request":
        evidence.append(("pull_request", str(artifact.pr), "linked"))
    evidence.append(("commit", commit_sha, "linked"))
    return PrDocument(
        chunk_id=chunk_id(repo_slug, path, None, content),
        kind=artifact.kind,
        path=path,
        content=content,
        content_sha=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        end_line=content.count("\n") + 1,
        lexical_tokens=tuple(lexical_tokens(content)),
        commit_sha=commit_sha,
        evidence=tuple(evidence),
    )


# --------------------------------------------------------------------- the database

_ARTIFACTS_SQL: Final = {
    "pull_request": (
        f"SELECT pr_number, NULL, title, body, author_is_bot FROM {PULL_REQUESTS} "
        "WHERE repo_id = %s"
    ),
    "review_comment": (
        f"SELECT pr_number, review_comment_id, NULL, body, author_is_bot FROM {REVIEW_COMMENTS} "
        "WHERE repo_id = %s"
    ),
    "issue_comment": f"""
        SELECT c.issue_number, c.comment_id, NULL, c.body, c.author_is_bot
        FROM {ISSUE_COMMENTS} c
        JOIN {ISSUES} i ON i.repo_id = c.repo_id AND i.issue_number = c.issue_number
        WHERE c.repo_id = %s AND i.is_pull_request
    """,
}
#: B2's link from each PR number to the one pool commit whose subject names it.
_LINKED_COMMITS_SQL: Final = f"""
    SELECT l.evidence_key::int, s.evidence_key
    FROM {CHUNK_EVIDENCE} l
    JOIN {CHUNK_EVIDENCE} s ON s.repo_id = l.repo_id AND s.chunk_id = l.chunk_id
    JOIN {CHUNKS} d ON d.chunk_id = l.chunk_id
    WHERE l.repo_id = %s AND d.kind = 'commit'
      AND l.source = 'linked' AND l.evidence_kind = 'pull_request'
      AND s.source = 'self' AND s.evidence_kind = 'commit'
"""
_SLUG_SQL: Final = f"SELECT owner || '/' || name FROM {REPOSITORIES} WHERE repo_id = %s"
_INSERT_DOCUMENT: Final = f"""
    INSERT INTO {CHUNKS} (
        chunk_id, repo_id, path, qualified_name, kind, language, start_line, end_line,
        content, content_sha, chunking_strategy_version, lexical_tokens, lexical_blob,
        first_seen_commit, last_seen_commit
    ) VALUES (
        %(chunk_id)s, %(repo_id)s, %(path)s, NULL, %(kind)s::artifact_kind, 'text', 1,
        %(end_line)s, %(content)s, %(content_sha)s, %(version)s, %(lexical_tokens)s,
        %(lexical_blob)s, %(sha)s, %(sha)s
    )
    ON CONFLICT (chunk_id) DO NOTHING
"""
_INSERT_EVIDENCE: Final = f"""
    INSERT INTO {CHUNK_EVIDENCE} (repo_id, chunk_id, evidence_kind, evidence_key, source)
    VALUES (%s, %s, %s::artifact_kind, %s, %s)
    ON CONFLICT (chunk_id, evidence_kind, evidence_key, source) DO NOTHING
"""
_COUNT_DOCUMENTS: Final = (
    f"SELECT count(*) FROM {CHUNKS} WHERE repo_id = %s AND kind = ANY(%s::artifact_kind[])"
)
_COUNT_EVIDENCE: Final = f"""
    SELECT count(*) FROM {CHUNK_EVIDENCE} e JOIN {CHUNKS} d USING (chunk_id)
    WHERE e.repo_id = %s AND d.kind = ANY(%s::artifact_kind[])
"""
_DUPLICATED: Final = f"""
    SELECT path FROM {CHUNKS} WHERE repo_id = %s AND kind = ANY(%s::artifact_kind[])
    GROUP BY path HAVING count(*) > 1 ORDER BY path
"""
_OUTSIDE: Final = f"""
    SELECT path FROM {CHUNKS}
    WHERE repo_id = %s AND kind = ANY(%s::artifact_kind[]) AND NOT (path = ANY(%s::text[]))
    ORDER BY path
"""


@dataclass
class PrPoolReport:
    documents_inserted: int = 0
    documents_skipped: int = 0
    evidence_inserted: int = 0
    evidence_skipped: int = 0
    left_out_bot: int = 0
    left_out_empty: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "documents": {"inserted": self.documents_inserted, "skipped": self.documents_skipped},
            "evidence": {"inserted": self.evidence_inserted, "skipped": self.evidence_skipped},
            "left_out": {"bot_authored": self.left_out_bot, "empty": self.left_out_empty},
            "by_kind": dict(sorted(self.by_kind.items())),
        }


def load_artifacts(conn: Any, repo_id: int) -> list[Artifact]:
    artifacts: list[Artifact] = []
    for kind, sql in _ARTIFACTS_SQL.items():
        for pr, artifact_id, title, body, bot in conn.execute(sql, (repo_id,)).fetchall():
            text = body if title is None else description_text(title, body)
            ident = None if artifact_id is None else int(artifact_id)
            artifacts.append(Artifact(kind, int(pr), ident, text, bool(bot)))
    return artifacts


def write_pr_pool(conn: Any, repo_id: int) -> PrPoolReport:
    """Every stored PR artifact as a document, in one transaction, under two invariants."""
    kinds = list(PR_KINDS)
    report = PrPoolReport()
    with conn.transaction():
        (slug,) = conn.execute(_SLUG_SQL, (repo_id,)).fetchone()
        rows = conn.execute(_LINKED_COMMITS_SQL, (repo_id,)).fetchall()
        linked = {int(pr): str(sha) for pr, sha in rows}
        documents: list[PrDocument] = []
        for artifact in load_artifacts(conn, repo_id):
            if artifact.pr not in linked:
                raise PoolInvariantError(
                    f"pull/{artifact.pr} is stored but B2 links no pool commit to it. "
                    "Nothing was written."
                )
            document = pr_document(artifact, commit_sha=linked[artifact.pr], repo_slug=slug)
            if document is None:
                if artifact.author_is_bot:
                    report.left_out_bot += 1
                else:
                    report.left_out_empty += 1
                continue
            documents.append(document)
        evidence = [(repo_id, d.chunk_id, *item) for d in documents for item in d.evidence]
        documents_before = _count(conn, _COUNT_DOCUMENTS, repo_id, kinds)
        evidence_before = _count(conn, _COUNT_EVIDENCE, repo_id, kinds)
        with conn.cursor() as cur:
            if documents:
                cur.executemany(_INSERT_DOCUMENT, [_params(repo_id, d) for d in documents])
            if evidence:
                cur.executemany(_INSERT_EVIDENCE, evidence)
        report.documents_inserted = (
            _count(conn, _COUNT_DOCUMENTS, repo_id, kinds) - documents_before
        )
        report.evidence_inserted = _count(conn, _COUNT_EVIDENCE, repo_id, kinds) - evidence_before
        report.documents_skipped = len(documents) - report.documents_inserted
        report.evidence_skipped = len(evidence) - report.evidence_inserted
        report.by_kind = dict(Counter(d.kind for d in documents))
        _check_invariants(conn, repo_id, [d.path for d in documents])
    return report


def _params(repo_id: int, d: PrDocument) -> dict[str, Any]:
    return {
        "chunk_id": d.chunk_id,
        "repo_id": repo_id,
        "path": d.path,
        "kind": d.kind,
        "end_line": d.end_line,
        "content": d.content,
        "content_sha": d.content_sha,
        "version": PR_STRATEGY_VERSION,
        "lexical_tokens": list(d.lexical_tokens),
        "lexical_blob": d.lexical_blob,
        "sha": d.commit_sha,
    }


def _count(conn: Any, sql: str, repo_id: int, kinds: Sequence[str]) -> int:
    row = conn.execute(sql, (repo_id, list(kinds))).fetchone()
    return int(row[0]) if row else 0


def _check_invariants(conn: Any, repo_id: int, paths: Sequence[str]) -> None:
    kinds = list(PR_KINDS)
    duplicated = [p for (p,) in conn.execute(_DUPLICATED, (repo_id, kinds)).fetchall()]
    if duplicated:
        raise PoolInvariantError(
            f"{len(duplicated):,} PR artifacts have more than one document (first: "
            f"{', '.join(duplicated[:5])}): a stale document from an earlier build is still "
            "stored. Nothing was written."
        )
    outside = [p for (p,) in conn.execute(_OUTSIDE, (repo_id, kinds, list(paths))).fetchall()]
    if outside:
        raise PoolInvariantError(
            f"{len(outside):,} PR documents match no stored artifact (first: "
            f"{', '.join(outside[:5])}). Nothing was written."
        )


def render_pr_report(report: PrPoolReport) -> str:
    kinds = ", ".join(f"{kind} {n:,}" for kind, n in sorted(report.by_kind.items())) or "none"
    return "\n".join(
        [
            f"PR documents: {report.documents_inserted:,} inserted, "
            f"{report.documents_skipped:,} already present ({kinds})",
            f"  left out: {report.left_out_bot:,} bot-written, {report.left_out_empty:,} empty",
        ]
    )

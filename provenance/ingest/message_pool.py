"""The benchmark's first pool: one document per in-scope commit message (ADR-0003).

Checkpoint B searches rationale text (ADR-0003 §1). This module writes the first of it:
the message of every first-parent commit that touches an in-scope file becomes a row in
`chunks` (kind 'commit', path 'commit/<sha>'), with `chunk_evidence` saying which commit
the document is (`self`) and which apache/airflow PR it belongs to (`linked`, §2). It is
the first writer of either table; B1's resolver reads them to turn a gold commit sha or
PR number into documents.

A PR number in a subject is linked only when it is an apache/airflow PR number. Both
filters were measured on the corpus (docs/tasks/eval-harness/002_commit_message_pool.md):

* Numbering. Before commit 1f63537c9b (2015-06-07), `Merge pull request #N` subjects
  number PRs in an earlier repository; airbnb/airflow, which apache/airflow inherited,
  restarted at #5 there. 138 references precede it.
* Plausibility. A number below 10% of the highest PR linked before it, in walk order,
  names a PR in a contributor's fork. One does, in 2018, at 0.8%; the next-lowest real
  PR sits at 45%.

Messages were scrubbed of email addresses at ingest (docs/tasks/ingest/004), so the
documents carry none.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from provenance.graph.ids import chunk_id, normalize_content
from provenance.graph.tables import CHUNK_EVIDENCE, CHUNKS, COMMITS, FILE_REVISIONS, REPOSITORIES
from provenance.parse.chunker import lexical_tokens

__all__ = [
    "APACHE_NUMBERING_STARTS",
    "DROPPED_BEFORE_NUMBERING",
    "DROPPED_IMPLAUSIBLE",
    "LINKED",
    "MESSAGE_STRATEGY_VERSION",
    "NO_REFERENCE",
    "NothingToBuild",
    "OUTCOMES",
    "PR_NUMBER_FLOOR",
    "PR_PATTERNS",
    "LinkDecision",
    "MessageDocument",
    "PoolCommit",
    "PoolInvariantError",
    "PoolReport",
    "link_decisions",
    "load_pool",
    "message_document",
    "pr_reference",
    "write_pool",
]

#: The first commit in apache/airflow's PR numbering (airbnb/airflow's, inherited):
#: "Merge pull request #5 from eerwitt/documentation_spelling", 2015-06-07, topo_order
#: 477 at corpus 1e2ad803f7. Earlier merge subjects number PRs in an earlier repository
#: (#3-#244 on mistercrunch/... branches), so their numbers name other PRs.
APACHE_NUMBERING_STARTS: Final = "1f63537c9b1105d0c61335cafc49305e534969d8"

#: A linked PR number must be at least this share of the highest PR linked before it.
#: Measured gap: the one fork-internal reference sits at 0.008, the next real PR at 0.45.
PR_NUMBER_FLOOR: Final = 0.10

#: chunks.chunking_strategy_version for message documents: provenance of derivation,
#: never part of the id (see graph/ids.py).
MESSAGE_STRATEGY_VERSION: Final = "commit-message-v1"

#: A merge commit's subject, then GitHub's squash suffix, anchored to the end.
PR_PATTERNS: Final = (r"^Merge pull request #(\d+)", r"\(#(\d+)\)\s*$")
_MERGE_PR_RE: Final = re.compile(PR_PATTERNS[0])
_SQUASH_PR_RE: Final = re.compile(PR_PATTERNS[1])

LINKED: Final = "linked"
NO_REFERENCE: Final = "no_reference"
DROPPED_BEFORE_NUMBERING: Final = "dropped_before_numbering"
DROPPED_IMPLAUSIBLE: Final = "dropped_implausible"
OUTCOMES: Final = (LINKED, NO_REFERENCE, DROPPED_BEFORE_NUMBERING, DROPPED_IMPLAUSIBLE)


@dataclass(frozen=True, slots=True)
class PoolCommit:
    """One commit of the pool, as the graph stores it."""

    sha: str
    topo_order: int
    subject: str
    message: str


@dataclass(frozen=True, slots=True)
class LinkDecision:
    sha: str
    pr: int | None
    outcome: str


@dataclass(frozen=True, slots=True)
class MessageDocument:
    """The `chunks` row for one commit message."""

    chunk_id: str
    sha: str
    path: str
    content: str
    content_sha: str
    end_line: int
    lexical_tokens: tuple[str, ...]

    @property
    def lexical_blob(self) -> str:
        """chunks.lexical_blob: the tokens space-joined, as the BM25 index reads them."""
        return " ".join(self.lexical_tokens)


def pr_reference(subject: str) -> int | None:
    """The PR a subject says produced its commit, or None.

    `Merge pull request #N from owner/branch` for a merge commit, else a trailing `(#N)`
    for GitHub's squash merge. The trailing match is anchored to the end, so in
    `Revert "X (#41856)" (#42193)` the commit came from #42193 and #41856 is quoted.
    """
    match = _MERGE_PR_RE.match(subject) or _SQUASH_PR_RE.search(subject)
    return int(match.group(1)) if match else None


def link_decisions(
    commits: Sequence[PoolCommit], *, numbering_starts_at: int
) -> list[LinkDecision]:
    """Decide, in walk order, which commits link to an apache/airflow PR.

    ``numbering_starts_at`` is the topo_order of APACHE_NUMBERING_STARTS. Only linked
    numbers raise the running maximum that the plausibility floor compares against.
    """
    decisions: list[LinkDecision] = []
    highest = 0
    for commit in sorted(commits, key=lambda c: c.topo_order):
        pr = pr_reference(commit.subject)
        if pr is None:
            outcome = NO_REFERENCE
        elif commit.topo_order < numbering_starts_at:
            outcome = DROPPED_BEFORE_NUMBERING
        elif pr < PR_NUMBER_FLOOR * highest:
            outcome = DROPPED_IMPLAUSIBLE
        else:
            outcome = LINKED
            highest = max(highest, pr)
        decisions.append(LinkDecision(commit.sha, pr, outcome))
    return decisions


def message_document(sha: str, message: str, *, repo_slug: str) -> MessageDocument:
    """Build the document for one commit message. Same input, same row, every time."""
    content = normalize_content(message)
    path = f"commit/{sha}"
    return MessageDocument(
        chunk_id=chunk_id(repo_slug, path, None, content),
        sha=sha,
        path=path,
        content=content,
        content_sha=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        end_line=content.count("\n") + 1,
        lexical_tokens=tuple(lexical_tokens(content)),
    )


# --------------------------------------------------------------------- the database


class PoolInvariantError(RuntimeError):
    """The written pool breaks a rule it must hold. Raised inside the build's
    transaction, so nothing the build wrote survives."""


class NothingToBuild(RuntimeError):
    """The graph holds no commit that touches an in-scope file: run `pace ingest` first."""


@dataclass
class PoolReport:
    """What one build wrote, and what it found already there."""

    commits: int = 0
    documents_inserted: int = 0
    documents_skipped: int = 0
    evidence_inserted: int = 0
    evidence_skipped: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "commits": self.commits,
            "documents": {"inserted": self.documents_inserted, "skipped": self.documents_skipped},
            "evidence": {"inserted": self.evidence_inserted, "skipped": self.evidence_skipped},
            "links": {outcome: self.outcomes.get(outcome, 0) for outcome in OUTCOMES},
        }


_POOL_SQL: Final = f"""
    SELECT c.commit_sha, c.topo_order, c.subject, c.message
    FROM {COMMITS} c
    WHERE c.repo_id = %s
      AND EXISTS (SELECT 1 FROM {FILE_REVISIONS} r
                  WHERE r.repo_id = c.repo_id AND r.commit_sha = c.commit_sha)
    ORDER BY c.topo_order
"""
_TOPO_SQL: Final = f"SELECT topo_order FROM {COMMITS} WHERE repo_id = %s AND commit_sha = %s"
_SLUG_SQL: Final = f"SELECT owner || '/' || name FROM {REPOSITORIES} WHERE repo_id = %s"
_INSERT_DOCUMENT: Final = f"""
    INSERT INTO {CHUNKS} (
        chunk_id, repo_id, path, qualified_name, kind, language, start_line, end_line,
        content, content_sha, chunking_strategy_version, lexical_tokens, lexical_blob,
        first_seen_commit, last_seen_commit
    ) VALUES (
        %(chunk_id)s, %(repo_id)s, %(path)s, NULL, 'commit', 'text', 1, %(end_line)s,
        %(content)s, %(content_sha)s, %(version)s, %(lexical_tokens)s, %(lexical_blob)s,
        %(sha)s, %(sha)s
    )
    ON CONFLICT (chunk_id) DO NOTHING
"""
_INSERT_EVIDENCE: Final = f"""
    INSERT INTO {CHUNK_EVIDENCE} (repo_id, chunk_id, evidence_kind, evidence_key, source)
    VALUES (%s, %s, %s::artifact_kind, %s, %s)
    ON CONFLICT (chunk_id, evidence_kind, evidence_key, source) DO NOTHING
"""
_COUNT_DOCUMENTS: Final = f"SELECT count(*) FROM {CHUNKS} WHERE repo_id = %s AND kind = 'commit'"
_COUNT_EVIDENCE: Final = f"""
    SELECT count(*) FROM {CHUNK_EVIDENCE} e JOIN {CHUNKS} d USING (chunk_id)
    WHERE e.repo_id = %s AND d.kind = 'commit'
"""
_DUPLICATED_DOCUMENTS: Final = f"""
    SELECT path FROM {CHUNKS} WHERE repo_id = %s AND kind = 'commit'
    GROUP BY path HAVING count(*) > 1 ORDER BY path
"""
_DOCUMENTS_OUTSIDE_POOL: Final = f"""
    SELECT path FROM {CHUNKS}
    WHERE repo_id = %s AND kind = 'commit' AND NOT (path = ANY(%s))
    ORDER BY path
"""


def load_pool(conn: Any, repo_id: int) -> list[PoolCommit]:
    """Every first-parent commit with a file_revisions row, in walk order (ADR-0003 §1)."""
    rows = conn.execute(_POOL_SQL, (repo_id,)).fetchall()
    return [PoolCommit(sha, int(topo), subject, message) for sha, topo, subject, message in rows]


def write_pool(
    conn: Any, repo_id: int, *, numbering_starts: str = APACHE_NUMBERING_STARTS
) -> PoolReport:
    """Write one document per pool commit, with its `self` and `linked` evidence.

    One transaction: the invariants are checked before it ends, so a build that breaks
    one writes nothing. Re-running inserts nothing new, and the report says what it
    skipped -- run 2 must skip exactly what run 1 inserted.
    """
    with conn.transaction():
        commits = load_pool(conn, repo_id)
        if not commits:
            raise NothingToBuild(
                f"repo {repo_id} has no commit that touches an in-scope file; "
                "run `pace ingest` first"
            )
        boundary = conn.execute(_TOPO_SQL, (repo_id, numbering_starts)).fetchone()
        if boundary is None:
            raise PoolInvariantError(
                f"the numbering boundary {numbering_starts} is not in the graph of repo "
                f"{repo_id}; without it no PR link can be trusted, so nothing was written"
            )
        (slug,) = conn.execute(_SLUG_SQL, (repo_id,)).fetchone()
        decided = link_decisions(commits, numbering_starts_at=int(boundary[0]))
        decisions = {decision.sha: decision for decision in decided}
        documents = [message_document(c.sha, c.message, repo_slug=slug) for c in commits]
        evidence = _evidence_rows(repo_id, documents, decisions)

        documents_before = _count(conn, _COUNT_DOCUMENTS, repo_id)
        evidence_before = _count(conn, _COUNT_EVIDENCE, repo_id)
        with conn.cursor() as cur:
            cur.executemany(_INSERT_DOCUMENT, [_document_params(repo_id, d) for d in documents])
            cur.executemany(_INSERT_EVIDENCE, evidence)
        documents_inserted = _count(conn, _COUNT_DOCUMENTS, repo_id) - documents_before
        evidence_inserted = _count(conn, _COUNT_EVIDENCE, repo_id) - evidence_before
        _check_invariants(conn, repo_id, documents)

    return PoolReport(
        commits=len(commits),
        documents_inserted=documents_inserted,
        documents_skipped=len(documents) - documents_inserted,
        evidence_inserted=evidence_inserted,
        evidence_skipped=len(evidence) - evidence_inserted,
        outcomes=Counter(decision.outcome for decision in decided),
    )


def _evidence_rows(
    repo_id: int, documents: Sequence[MessageDocument], decisions: dict[str, LinkDecision]
) -> list[tuple[int, str, str, str, str]]:
    rows: list[tuple[int, str, str, str, str]] = []
    for doc in documents:
        rows.append((repo_id, doc.chunk_id, "commit", doc.sha, "self"))
        decision = decisions[doc.sha]
        if decision.outcome == LINKED:
            rows.append((repo_id, doc.chunk_id, "pull_request", str(decision.pr), "linked"))
    return rows


def _document_params(repo_id: int, doc: MessageDocument) -> dict[str, Any]:
    return {
        "chunk_id": doc.chunk_id,
        "repo_id": repo_id,
        "path": doc.path,
        "end_line": doc.end_line,
        "content": doc.content,
        "content_sha": doc.content_sha,
        "version": MESSAGE_STRATEGY_VERSION,
        "lexical_tokens": list(doc.lexical_tokens),
        "lexical_blob": doc.lexical_blob,
        "sha": doc.sha,
    }


def _count(conn: Any, sql: str, repo_id: int) -> int:
    row = conn.execute(sql, (repo_id,)).fetchone()
    return int(row[0]) if row else 0


def _check_invariants(conn: Any, repo_id: int, documents: Sequence[MessageDocument]) -> None:
    duplicated = [path for (path,) in conn.execute(_DUPLICATED_DOCUMENTS, (repo_id,)).fetchall()]
    if duplicated:
        raise PoolInvariantError(
            f"{len(duplicated):,} commits have more than one message document (first: "
            f"{', '.join(duplicated[:5])}): a stale document from an earlier build is "
            "still stored. Nothing was written."
        )
    paths = [doc.path for doc in documents]
    outside = [
        path for (path,) in conn.execute(_DOCUMENTS_OUTSIDE_POOL, (repo_id, paths)).fetchall()
    ]
    if outside:
        raise PoolInvariantError(
            f"{len(outside):,} message documents belong to no pool commit (first: "
            f"{', '.join(outside[:5])}). Nothing was written."
        )

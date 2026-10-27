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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from provenance.graph.ids import chunk_id, normalize_content
from provenance.parse.chunker import lexical_tokens

__all__ = [
    "APACHE_NUMBERING_STARTS",
    "DROPPED_BEFORE_NUMBERING",
    "DROPPED_IMPLAUSIBLE",
    "LINKED",
    "MESSAGE_STRATEGY_VERSION",
    "NO_REFERENCE",
    "OUTCOMES",
    "PR_NUMBER_FLOOR",
    "PR_PATTERNS",
    "LinkDecision",
    "MessageDocument",
    "PoolCommit",
    "link_decisions",
    "message_document",
    "pr_reference",
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

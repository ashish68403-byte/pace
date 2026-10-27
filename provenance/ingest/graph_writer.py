"""Persist the history walk into the graph: commits and the links between them.

What this module is for: ``git_walk.walk_history`` streams commits newest first.
This module writes them -- idempotently, with counts that prove it -- and then
checks that the graph it left behind is the one the walk described. Specified in
docs/tasks/ingest/001_commit_graph.md.

Rules, each one a failure this module exists to prevent:

1. Every walked commit is written, not only in-scope ones. ``commit_parents`` has a
   foreign key on both ends, so scoping commits would leave most links pointing at
   rows that never exist.
2. Parent links are written after the walk. The walk runs newest -> oldest (rename
   resolution needs that order), so a parent's row does not exist yet when its
   child is written.
3. Conflict targets are always named. A bare ``ON CONFLICT DO NOTHING`` swallows
   every unique violation -- including ``commits_topo_unique``, the one that means
   the numbering is wrong.
4. One commit, one savepoint. A bad row rolls back only its own commit. Without the
   savepoint Postgres aborts the whole transaction ("current transaction is
   aborted, commands ignored until end of transaction block") and every later row
   in the batch fails with it.
5. Counts mean one thing each: inserted = created by this run, already present =
   existed before it. Links that cannot be written are reported separately and
   never folded into either.
6. The run checks itself. After the walk the stored graph is compared with what the
   walk saw, and a disagreement fails the run instead of becoming a log line.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from typing import Any, Final

import psycopg

from provenance.graph.tables import COMMIT_PARENTS, COMMITS
from provenance.ingest.git_walk import (
    CommitRecord,
    CommitRejected,
    FileChange,
    IngestInvariantError,
    RowCounts,
)

__all__ = ["GraphWriter", "WriterStats"]

# Errors that mean "this one commit's data is unacceptable": a constraint it breaks,
# a value the schema rejects, a change this module cannot represent. Anything else --
# a lost connection, SQL that does not match the schema -- means the program or the
# database is broken, and must stop the walk instead of dead-lettering 38,888
# commits one at a time.
_ROW_ERRORS: Final = (psycopg.IntegrityError, psycopg.DataError, ValueError)

_INSERT_COMMIT: Final = f"""
    INSERT INTO {COMMITS} (
        repo_id, commit_sha, tree_sha, topo_order,
        author_name, author_email, authored_at,
        committer_name, committer_email, committed_at,
        subject, message, files_changed, is_merge, is_mechanical, mechanical_reason
    ) VALUES (
        %(repo_id)s, %(commit_sha)s, %(tree_sha)s, %(topo_order)s,
        %(author_name)s, %(author_email)s, %(authored_at)s,
        %(committer_name)s, %(committer_email)s, %(committed_at)s,
        %(subject)s, %(message)s, %(files_changed)s, %(is_merge)s, %(is_mechanical)s,
        %(mechanical_reason)s
    )
    ON CONFLICT (repo_id, commit_sha) DO NOTHING
"""

_INSERT_PARENT: Final = f"""
    INSERT INTO {COMMIT_PARENTS} (repo_id, child_sha, parent_index, parent_sha)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (repo_id, child_sha, parent_index) DO NOTHING
"""

_STORED_COMMITS: Final = f"""
    SELECT commit_sha FROM {COMMITS} WHERE repo_id = %s AND commit_sha = ANY(%s::text[])
"""

# Every walked commit that is already stored must carry the number this walk gave
# it. ON CONFLICT DO NOTHING keeps an old row exactly as it was, so without this
# check a run that numbered differently would leave its numbers behind silently.
_TOPO_MISMATCHES: Final = f"""
    SELECT count(*)
      FROM unnest(%s::text[], %s::bigint[]) AS walked(sha, topo_order)
      JOIN {COMMITS} AS c ON c.repo_id = %s AND c.commit_sha = walked.sha
     WHERE c.topo_order <> walked.topo_order
"""

_FIRST_PARENT_LINKS: Final = f"""
    SELECT count(*) FROM {COMMIT_PARENTS}
     WHERE repo_id = %s AND parent_index = 0 AND child_sha = ANY(%s::text[])
"""


@dataclass(frozen=True, slots=True)
class WalkedCommit:
    """What finish() needs to remember about each commit the walk produced."""

    sha: str
    topo_order: int
    parents: tuple[str, ...]


@dataclass(slots=True)
class WriterStats:
    """Everything one run did, per table. ``report()`` is the public view of it."""

    commits_inserted: int = 0
    commits_present: int = 0
    commits_rejected: int = 0
    links_inserted: int = 0
    links_present: int = 0
    links_to_unwalked_parents: int = 0
    links_failed: int = 0

    def add(self, other: WriterStats) -> None:
        for spec in fields(self):
            setattr(self, spec.name, getattr(self, spec.name) + getattr(other, spec.name))


class GraphWriter:
    """Writes walk records into the graph. Implements ``git_walk.CommitWriter``.

    Holds one non-autocommit connection for the whole walk. Each commit runs in its
    own savepoint, and the surrounding transaction commits every ``batch_size``
    commits -- so a crash loses at most one batch, and a re-run recovers it, because
    every write is idempotent.
    """

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        *,
        repo_id: int,
        batch_size: int = 250,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {batch_size}")
        if conn.autocommit:
            raise ValueError("GraphWriter needs a non-autocommit connection for its savepoints")
        self.conn = conn
        self.repo_id = repo_id
        self.batch_size = batch_size
        self.stats = WriterStats()
        self._walked: list[WalkedCommit] = []
        self._rejected: set[str] = set()
        self._since_commit = 0

    # -- CommitWriter --------------------------------------------------------

    def write(self, record: CommitRecord, resolved: Sequence[tuple[FileChange, str]]) -> RowCounts:
        """Write one commit, all or nothing. (``resolved`` is written from task 002 on.)"""
        if record.topo_order is None:
            raise ValueError(f"{record.sha} has no topo_order; walk_history assigns it")
        self._walked.append(WalkedCommit(record.sha, record.topo_order, tuple(record.parents)))
        delta = WriterStats()

        self.conn.execute("SAVEPOINT pace_commit")
        try:
            if self.conn.execute(_INSERT_COMMIT, self._commit_params(record)).rowcount == 1:
                delta.commits_inserted = 1
            else:
                delta.commits_present = 1
        except _ROW_ERRORS as exc:
            self.conn.execute("ROLLBACK TO SAVEPOINT pace_commit")
            self._rejected.add(record.sha)
            self.stats.commits_rejected += 1
            raise CommitRejected(record.sha, exc) from exc
        self.conn.execute("RELEASE SAVEPOINT pace_commit")

        self.stats.add(delta)
        self._since_commit += 1
        if self._since_commit >= self.batch_size:
            self.conn.commit()
            self._since_commit = 0
        return RowCounts(inserted=delta.commits_inserted, skipped=delta.commits_present)

    def finish(self, *, walk_complete: bool) -> RowCounts:
        """Write the parent links, then check the stored graph against the walk."""
        self.conn.commit()
        self._since_commit = 0
        stored = self._stored(sha for w in self._walked for sha in (w.sha, *w.parents))
        for walked in self._walked:
            for index, parent in enumerate(walked.parents):
                if walked.sha in self._rejected or parent in self._rejected:
                    self.stats.links_failed += 1
                elif walked.sha not in stored or parent not in stored:
                    self.stats.links_to_unwalked_parents += 1
                elif self._insert_link(walked.sha, index, parent):
                    self.stats.links_inserted += 1
                else:
                    self.stats.links_present += 1
        self.conn.commit()
        self._check(walk_complete=walk_complete, stored=stored)
        return RowCounts(inserted=self.stats.links_inserted, skipped=self.stats.links_present)

    def report(self) -> dict[str, dict[str, int]]:
        s = self.stats
        return {
            "commits": {
                "inserted": s.commits_inserted,
                "already_present": s.commits_present,
                "rejected": s.commits_rejected,
            },
            "commit_parents": {
                "inserted": s.links_inserted,
                "already_present": s.links_present,
                "to_unwalked_parents": s.links_to_unwalked_parents,
                "failed": s.links_failed,
            },
        }

    def summary(self) -> str:
        report = self.report()
        c, p = report["commits"], report["commit_parents"]
        return "\n".join(
            [
                f"commits:        {c['inserted']:,} inserted, {c['already_present']:,} already "
                f"present, {c['rejected']:,} rejected",
                f"parent links:   {p['inserted']:,} inserted, {p['already_present']:,} already "
                f"present, {p['to_unwalked_parents']:,} to unwalked parents, "
                f"{p['failed']:,} failed",
            ]
        )

    # -- internals -----------------------------------------------------------

    def _commit_params(self, record: CommitRecord) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "commit_sha": record.sha,
            "tree_sha": record.tree_sha,
            "topo_order": record.topo_order,
            "author_name": record.author_name,
            "author_email": record.author_email,
            "authored_at": record.author_time,
            "committer_name": record.committer_name,
            "committer_email": record.committer_email,
            "committed_at": record.committer_time,
            "subject": record.subject,
            "message": record.message,
            "files_changed": len(record.changes),
            "is_merge": len(record.parents) > 1,
            "is_mechanical": record.mechanical,
            "mechanical_reason": record.mechanical_reason,
        }

    def _insert_link(self, child: str, index: int, parent: str) -> bool:
        cursor = self.conn.execute(_INSERT_PARENT, (self.repo_id, child, index, parent))
        return cursor.rowcount == 1

    def _stored(self, shas: Iterable[str]) -> set[str]:
        rows = self.conn.execute(_STORED_COMMITS, (self.repo_id, sorted(set(shas)))).fetchall()
        return {str(row[0]) for row in rows}

    def _count(self, sql: str, params: tuple[Any, ...]) -> int:
        row = self.conn.execute(sql, params).fetchone()
        return int(row[0]) if row is not None else 0

    def _check(self, *, walk_complete: bool, stored: set[str]) -> None:
        problems: list[str] = []
        unaccounted = [
            w.sha for w in self._walked if w.sha not in stored and w.sha not in self._rejected
        ]
        if unaccounted:
            problems.append(
                f"{len(unaccounted):,} walked commits were neither stored nor dead-lettered "
                f"(first: {unaccounted[0]})"
            )
        shas = [w.sha for w in self._walked]
        topo = [w.topo_order for w in self._walked]
        mismatched = self._count(_TOPO_MISMATCHES, (shas, topo, self.repo_id))
        if mismatched:
            problems.append(
                f"{mismatched:,} stored commits carry a different topo_order from this walk: "
                "an earlier run numbered them differently, and ON CONFLICT DO NOTHING kept "
                "its numbers"
            )
        usable = stored - self._rejected
        expected = [
            w.sha for w in self._walked if w.sha in usable and w.parents and w.parents[0] in usable
        ]
        linked = self._count(_FIRST_PARENT_LINKS, (self.repo_id, expected))
        if linked != len(expected):
            problems.append(
                f"{len(expected):,} walked commits have a stored first parent, but only "
                f"{linked:,} first-parent links exist"
            )
        if problems:
            raise IngestInvariantError("; ".join(problems))

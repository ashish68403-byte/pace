"""Persist the history walk into the graph: commits, parent links, files, revisions.

What this module is for: ``git_walk.walk_history`` streams rename-resolved commits
newest first. This module writes them -- idempotently, with counts that prove it --
and then checks that the graph it left behind is the one the walk described.
Specified in docs/tasks/ingest/001_commit_graph.md and 002_file_history.md.

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
4. One commit, one savepoint. A bad row rolls back only its own commit and its
   revisions. Without the savepoint Postgres aborts the whole transaction ("current
   transaction is aborted, commands ignored until end of transaction block") and
   every later row in the batch fails with it.
5. Counts mean one thing each: inserted = created by this run, already present =
   existed before it. Links that cannot be written are reported separately and
   never folded into either.
6. The run checks itself. After the walk the stored graph is compared with what the
   walk saw, and a disagreement fails the run instead of becoming a log line.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from typing import Any, Final

import psycopg

from provenance.graph.tables import COMMIT_PARENTS, COMMITS, FILE_REVISIONS, FILES
from provenance.ingest.git_walk import (
    CommitRecord,
    CommitRejected,
    FileChange,
    IngestInvariantError,
    RowCounts,
)

__all__ = [
    "CHANGE_TYPES",
    "GraphWriter",
    "WriterStats",
    "one_change_per_file",
    "path_at_commit",
    "revision_row",
]

# Errors that mean "this one commit's data is unacceptable": a constraint it breaks,
# a value the schema rejects, a change this module cannot represent. Anything else --
# a lost connection, SQL that does not match the schema -- means the program or the
# database is broken, and must stop the walk instead of dead-lettering 38,888
# commits one at a time.
_ROW_ERRORS: Final = (psycopg.IntegrityError, psycopg.DataError, ValueError)

#: git's status letter -> the `file_change` enum in 0001_core.
CHANGE_TYPES: Final[dict[str, str]] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type_changed",
}

_SHA_RE: Final = re.compile(r"[0-9a-f]{40}")
_ZERO_SHA: Final = "0" * 40

_INSERT_COMMIT: Final = f"""
    INSERT INTO {COMMITS} (
        repo_id, commit_sha, tree_sha, topo_order,
        author_name, author_email_sha256, authored_at,
        committer_name, committer_email_sha256, committed_at,
        subject, message, files_changed, is_merge, is_mechanical, mechanical_reason
    ) VALUES (
        %(repo_id)s, %(commit_sha)s, %(tree_sha)s, %(topo_order)s,
        %(author_name)s, %(author_email_sha256)s, %(authored_at)s,
        %(committer_name)s, %(committer_email_sha256)s, %(committed_at)s,
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

_SELECT_FILES: Final = f"SELECT path, file_id FROM {FILES} WHERE repo_id = %s"

_SELECT_FILE_ID: Final = f"SELECT file_id FROM {FILES} WHERE repo_id = %s AND path = %s"

_INSERT_FILE: Final = f"""
    INSERT INTO {FILES} (repo_id, path, language)
    VALUES (%s, %s, %s)
    ON CONFLICT (repo_id, path) DO NOTHING
    RETURNING file_id
"""

_INSERT_REVISION: Final = f"""
    INSERT INTO {FILE_REVISIONS} (
        repo_id, file_id, commit_sha, commit_topo_order, blob_sha,
        change_type, old_path, path_at_commit, similarity
    ) VALUES (
        %(repo_id)s, %(file_id)s, %(commit_sha)s, %(commit_topo_order)s, %(blob_sha)s,
        %(change_type)s::file_change, %(old_path)s, %(path_at_commit)s, %(similarity)s
    )
    ON CONFLICT (file_id, commit_sha) DO NOTHING
"""

# Touches only rows whose values change, so a re-run reports 0 updated.
_UPDATE_FILE_SPANS: Final = f"""
    UPDATE {FILES} AS f
       SET first_seen_commit = span.first_sha,
           last_seen_commit  = span.last_sha
      FROM (
            SELECT file_id,
                   (array_agg(commit_sha ORDER BY commit_topo_order ASC))[1]  AS first_sha,
                   (array_agg(commit_sha ORDER BY commit_topo_order DESC))[1] AS last_sha
              FROM {FILE_REVISIONS}
             WHERE repo_id = %s
             GROUP BY file_id
           ) AS span
     WHERE f.file_id = span.file_id
       AND (f.first_seen_commit IS DISTINCT FROM span.first_sha
            OR f.last_seen_commit IS DISTINCT FROM span.last_sha)
"""

_FILES_IN_SCOPE: Final = f"""
    SELECT path FROM {FILES} WHERE repo_id = %s AND path = ANY(%s::text[])
"""

_FILES_WITHOUT_REVISIONS: Final = f"""
    SELECT f.path FROM {FILES} AS f
     WHERE f.repo_id = %s AND f.path = ANY(%s::text[])
       AND NOT EXISTS (SELECT 1 FROM {FILE_REVISIONS} AS r WHERE r.file_id = f.file_id)
     ORDER BY f.path
"""

_FILES_ENDING_DELETED: Final = f"""
    SELECT f.path
      FROM {FILES} AS f
      JOIN LATERAL (
            SELECT r.change_type FROM {FILE_REVISIONS} AS r
             WHERE r.file_id = f.file_id
             ORDER BY r.commit_topo_order DESC
             LIMIT 1
           ) AS newest ON true
     WHERE f.repo_id = %s AND f.path = ANY(%s::text[]) AND newest.change_type = 'deleted'
     ORDER BY f.path
"""


# --------------------------------------------------------------------- the rules


def path_at_commit(change: FileChange) -> str:
    """The path a change touched as of its commit: the new path, or the old one for a delete."""
    path = change.new_path if change.new_path is not None else change.old_path
    if path is None:
        raise ValueError(f"a {change.status} change with no path")
    return path


def revision_row(change: FileChange) -> dict[str, Any]:
    """The ``file_revisions`` columns one change determines."""
    letter = change.status[:1]
    change_type = CHANGE_TYPES.get(letter)
    if change_type is None:
        raise ValueError(f"unknown git status {change.status!r} on {path_at_commit(change)}")
    # A deletion's new blob is git's all-zero placeholder. The content it removed is
    # the old blob, and that is what the revision points at.
    blob = change.old_blob if letter == "D" else change.new_blob
    if blob is None or blob == _ZERO_SHA or not _SHA_RE.fullmatch(blob):
        raise ValueError(f"no usable blob for {change.status} {path_at_commit(change)}: {blob!r}")
    moved = letter in {"R", "C"}
    return {
        "blob_sha": blob,
        "change_type": change_type,
        "old_path": change.old_path if moved else None,
        "path_at_commit": path_at_commit(change),
        "similarity": change.similarity if moved else None,
    }


def one_change_per_file(
    resolved: Sequence[tuple[FileChange, str]],
) -> tuple[list[tuple[FileChange, str]], int]:
    """Keep one change per file; return the survivors and how many were collapsed.

    ``UNIQUE (file_id, commit_sha)`` allows one revision per file per commit. When
    two changes in one commit resolve to the same file, keep the one whose path is
    already today's path, else the first in git's order. A fixed rule keeps re-runs
    identical; the count keeps the collapse visible.
    """
    chosen: dict[str, tuple[FileChange, str]] = {}
    collapsed = 0
    for change, canonical in resolved:
        current = chosen.get(canonical)
        if current is None:
            chosen[canonical] = (change, canonical)
            continue
        collapsed += 1
        if path_at_commit(current[0]) != canonical and path_at_commit(change) == canonical:
            chosen[canonical] = (change, canonical)
    return list(chosen.values()), collapsed


def _language(path: str) -> str | None:
    return "python" if path.endswith(".py") else None


# --------------------------------------------------------------------- the writer


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
    files_inserted: int = 0
    files_present: int = 0
    file_spans_updated: int = 0
    revisions_inserted: int = 0
    revisions_present: int = 0
    collapsed_changes: int = 0

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
        scope: Sequence[str] | None = None,
        batch_size: int = 250,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {batch_size}")
        if conn.autocommit:
            raise ValueError("GraphWriter needs a non-autocommit connection for its savepoints")
        self.conn = conn
        self.repo_id = repo_id
        self.scope: list[str] | None = list(scope) if scope is not None else None
        self.batch_size = batch_size
        self.stats = WriterStats()
        self._walked: list[WalkedCommit] = []
        self._rejected: set[str] = set()
        self._since_commit = 0
        rows = conn.execute(_SELECT_FILES, (repo_id,)).fetchall()
        self._file_ids: dict[str, int] = {str(path): int(file_id) for path, file_id in rows}
        self._initial_files: frozenset[str] = frozenset(self._file_ids)
        self._touched_files: set[str] = set()

    # -- CommitWriter --------------------------------------------------------

    def write(self, record: CommitRecord, resolved: Sequence[tuple[FileChange, str]]) -> RowCounts:
        """Write one commit and its in-scope revisions: all of them, or none."""
        if record.topo_order is None:
            raise ValueError(f"{record.sha} has no topo_order; walk_history assigns it")
        self._walked.append(WalkedCommit(record.sha, record.topo_order, tuple(record.parents)))
        delta = WriterStats()
        new_files: dict[str, int] = {}
        kept: list[tuple[FileChange, str]] = []

        self.conn.execute("SAVEPOINT pace_commit")
        try:
            if self.conn.execute(_INSERT_COMMIT, self._commit_params(record)).rowcount == 1:
                delta.commits_inserted = 1
            else:
                delta.commits_present = 1
            kept, delta.collapsed_changes = one_change_per_file(resolved)
            for change, canonical in kept:
                file_id = self._file_ids.get(canonical, new_files.get(canonical))
                if file_id is None:
                    file_id, created = self._insert_file(canonical)
                    new_files[canonical] = file_id
                    delta.files_inserted += int(created)
                row = {
                    **revision_row(change),
                    "repo_id": self.repo_id,
                    "file_id": file_id,
                    "commit_sha": record.sha,
                    "commit_topo_order": record.topo_order,
                }
                if self.conn.execute(_INSERT_REVISION, row).rowcount == 1:
                    delta.revisions_inserted += 1
                else:
                    delta.revisions_present += 1
        except _ROW_ERRORS as exc:
            self.conn.execute("ROLLBACK TO SAVEPOINT pace_commit")
            self._rejected.add(record.sha)
            self.stats.commits_rejected += 1
            raise CommitRejected(record.sha, exc) from exc
        self.conn.execute("RELEASE SAVEPOINT pace_commit")

        # Only now is anything this commit created real: a rolled-back savepoint must
        # not leave a file_id in the cache that the database never kept.
        self._file_ids.update(new_files)
        self._touched_files.update(canonical for _, canonical in kept)
        self.stats.add(delta)
        self._since_commit += 1
        if self._since_commit >= self.batch_size:
            self.conn.commit()
            self._since_commit = 0
        return RowCounts(
            inserted=delta.commits_inserted + delta.files_inserted + delta.revisions_inserted,
            skipped=delta.commits_present + delta.revisions_present,
        )

    def finish(self, *, walk_complete: bool) -> RowCounts:
        """Write the parent links and file spans, then check the stored graph against the walk."""
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
        self.stats.file_spans_updated = self.conn.execute(
            _UPDATE_FILE_SPANS, (self.repo_id,)
        ).rowcount
        self.stats.files_present = len(self._touched_files & self._initial_files)
        self.conn.commit()
        self._check(walk_complete=walk_complete, stored=stored)
        return RowCounts(
            inserted=self.stats.links_inserted,
            updated=self.stats.file_spans_updated,
            skipped=self.stats.links_present + self.stats.files_present,
        )

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
            "files": {
                "inserted": s.files_inserted,
                "already_present": s.files_present,
                "spans_updated": s.file_spans_updated,
            },
            "file_revisions": {
                "inserted": s.revisions_inserted,
                "already_present": s.revisions_present,
                "collapsed_changes": s.collapsed_changes,
            },
        }

    def summary(self) -> str:
        report = self.report()
        c, p = report["commits"], report["commit_parents"]
        f, v = report["files"], report["file_revisions"]
        return "\n".join(
            [
                f"commits:        {c['inserted']:,} inserted, {c['already_present']:,} already "
                f"present, {c['rejected']:,} rejected",
                f"parent links:   {p['inserted']:,} inserted, {p['already_present']:,} already "
                f"present, {p['to_unwalked_parents']:,} to unwalked parents, "
                f"{p['failed']:,} failed",
                f"files:          {f['inserted']:,} inserted, {f['already_present']:,} already "
                f"present, {f['spans_updated']:,} first/last commit updated",
                f"file revisions: {v['inserted']:,} inserted, {v['already_present']:,} already "
                f"present, {v['collapsed_changes']:,} collapsed",
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
            "author_email_sha256": record.author_email_sha256,
            "authored_at": record.author_time,
            "committer_name": record.committer_name,
            "committer_email_sha256": record.committer_email_sha256,
            "committed_at": record.committer_time,
            "subject": record.subject,
            "message": record.message,
            "files_changed": len(record.changes),
            "is_merge": len(record.parents) > 1,
            "is_mechanical": record.mechanical,
            "mechanical_reason": record.mechanical_reason,
        }

    def _insert_file(self, path: str) -> tuple[int, bool]:
        """Create the files row for ``path``; return (file_id, created by this call)."""
        row = self.conn.execute(_INSERT_FILE, (self.repo_id, path, _language(path))).fetchone()
        if row is not None:
            return int(row[0]), True
        # Someone else created it after this run loaded its cache.
        existing = self.conn.execute(_SELECT_FILE_ID, (self.repo_id, path)).fetchone()
        if existing is None:  # pragma: no cover - the conflict above means it exists
            raise RuntimeError(f"files row for {path} was neither inserted nor found")
        return int(existing[0]), False

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
        if walk_complete and self.scope is not None:
            problems.extend(self._file_problems(self.scope))
        if problems:
            raise IngestInvariantError("; ".join(problems))

    def _file_problems(self, scope: list[str]) -> list[str]:
        """The three properties every in-scope file must have after a complete walk."""
        problems: list[str] = []
        rows = self.conn.execute(_FILES_IN_SCOPE, (self.repo_id, scope)).fetchall()
        absent = sorted(set(scope) - {str(row[0]) for row in rows})
        if absent:
            problems.append(
                f"{len(absent):,} in-scope paths have no files row (first: {absent[0]})"
            )
        bare = [
            row[0] for row in self.conn.execute(_FILES_WITHOUT_REVISIONS, (self.repo_id, scope))
        ]
        if bare:
            problems.append(f"{len(bare):,} in-scope files have no revisions (first: {bare[0]})")
        ended = [row[0] for row in self.conn.execute(_FILES_ENDING_DELETED, (self.repo_id, scope))]
        if ended:
            problems.append(
                f"{len(ended):,} in-scope files end in a deletion, but scope lists files that "
                f"exist (first: {ended[0]})"
            )
        return problems

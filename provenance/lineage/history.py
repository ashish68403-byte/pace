"""A repository's file histories in memory, as checkpoint A stores them (slice C3b).

docs/tasks/function-identity/003_oracle_run.md: CodeShovel's ten projects were never ingested,
and ingesting them would store their commit authors' names, while DATA-PROTECTION.md limits PACE
to apache/airflow. So the oracle run rebuilds each file's rename-aware, first-parent history in
memory with checkpoint A's own pieces: `raw_log_stream`, `RenameChains`, `one_change_per_file`
and `revision_row`, numbered as `walk_history` numbers commits. Tests check that the result
equals the `file_revisions` rows `pace ingest` writes, and that pass 1 over it equals the build's.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from provenance.ingest.git_walk import (
    CatFileBatch,
    FileChange,
    RenameChains,
    _assert_not_shallow,
    first_parent_count,
    raw_log_stream,
)
from provenance.ingest.graph_writer import one_change_per_file, revision_row
from provenance.lineage.build import Cache, symbols_at
from provenance.lineage.match import Revision, Symbol

PYTHON_SUFFIX: Final = ".py"


class HistoryError(RuntimeError):
    """The log stream did not produce the history git counts."""


@dataclass(frozen=True)
class FileRevision:
    """One `file_revisions` row: a file at one commit."""

    commit: str
    topo: int
    change_type: str
    blob: str
    path: str


@dataclass(frozen=True)
class History:
    """Every file's revisions from one start commit, oldest first, keyed by its path there."""

    start: str
    files: Mapping[str, tuple[FileRevision, ...]]
    #: The Python files each commit touched (path at that commit ends in .py), by path at start.
    python_touched: Mapping[str, tuple[str, ...]]
    #: Commits checkpoint A's writer would reject whole: a change with no usable blob.
    rejected: tuple[str, ...]


def read_history(repo: Path, start: str) -> History:
    """The first-parent history from `start`, walked newest first as `walk_history` walks it."""
    _assert_not_shallow(repo)
    total = first_parent_count(repo, start)
    chains = RenameChains()
    files: dict[str, list[FileRevision]] = defaultdict(list)
    touched: dict[str, tuple[str, ...]] = {}
    rejected: list[str] = []
    walked = 0
    for position, record in enumerate(raw_log_stream(repo, rev=start)):
        walked += 1
        topo = total - position
        resolved: list[tuple[FileChange, str]] = []
        for change in record.changes:
            if change.is_rename and change.old_path and change.new_path:
                chains.record(change.old_path, change.new_path)
            resolved.append((change, chains.resolve(change.new_path or change.old_path or "")))
        kept, _ = one_change_per_file(resolved)
        try:
            rows = [(canonical, revision_row(change)) for change, canonical in kept]
        except ValueError:  # GraphWriter rejects the whole commit (CommitRejected)
            rejected.append(record.sha)
            continue
        for canonical, row in rows:
            files[canonical].append(
                FileRevision(
                    record.sha, topo, row["change_type"], row["blob_sha"], row["path_at_commit"]
                )
            )
        touched[record.sha] = tuple(
            sorted(
                canonical
                for canonical, row in rows
                if str(row["path_at_commit"]).endswith(PYTHON_SUFFIX)
            )
        )
    if walked != total:
        raise HistoryError(
            f"git rev-list counts {total:,} first-parent commits from {start}, but the log "
            f"stream produced {walked:,}"
        )
    return History(
        start=start,
        files={path: tuple(reversed(found)) for path, found in sorted(files.items())},
        python_touched=touched,
        rejected=tuple(rejected),
    )


def revisions_reader(
    history: History, cat: CatFileBatch, cache: Cache
) -> Callable[[str], list[Revision]]:
    """Each file's revisions parsed for pass 1 as `build.file_passes` parses them, read once."""
    parsed: dict[str, list[Revision]] = {}

    def read(path: str) -> list[Revision]:
        if path not in parsed:
            revisions: list[Revision] = []
            for found in history.files[path]:
                symbols: dict[str, Symbol] | None = {}
                if found.change_type != "deleted":
                    data = cat.read(found.blob)
                    text = None if data is None else data.decode("utf-8", errors="replace")
                    symbols = None if text is None else symbols_at(text, found.path, cache)
                revisions.append(Revision(found.commit, found.topo, found.path, symbols))
            parsed[path] = revisions
        return parsed[path]

    return read

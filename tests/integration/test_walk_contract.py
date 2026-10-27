"""What walk_history promises a writer: every commit, numbered from the root, and
a clear line between "dead-letter this commit" and "stop the walk"."""

from __future__ import annotations

from typing import Any

import pytest

from provenance.ingest import git_walk
from provenance.ingest.git_walk import (
    CommitRejected,
    IngestInvariantError,
    RowCounts,
    walk_history,
)

pytestmark = pytest.mark.db


class RecordingWriter:
    def __init__(
        self, *, reject: frozenset[str] = frozenset(), explode: frozenset[str] = frozenset()
    ) -> None:
        self.calls: list[tuple[str, int | None, list[str]]] = []
        self.reject = reject
        self.explode = explode
        self.finished_with: bool | None = None

    def write(self, record: Any, resolved: Any) -> RowCounts:
        self.calls.append((record.sha, record.topo_order, [canonical for _, canonical in resolved]))
        if record.sha in self.reject:
            raise CommitRejected(record.sha, ValueError("rejected by the test"))
        if record.sha in self.explode:
            raise RuntimeError("the database went away")
        return RowCounts(inserted=1)

    def finish(self, *, walk_complete: bool) -> RowCounts:
        self.finished_with = walk_complete
        return RowCounts()

    def report(self) -> dict[str, Any]:
        return {}


def _walk(repo: Any, repo_id: int, writer: Any, **kwargs: Any) -> Any:
    return walk_history(repo.root, scope=repo.scope, repo_id=repo_id, writer=writer, **kwargs)


def test_every_commit_reaches_the_writer_numbered_from_the_root(history_repo, test_repo_id):
    writer = RecordingWriter()
    result, _ = _walk(history_repo, test_repo_id, writer)
    assert [(history_repo.name_of(sha), topo) for sha, topo, _ in writer.calls] == [
        ("c9", 9),
        ("c8", 8),
        ("c7", 7),
        ("c6", 6),
        ("m5", 5),
        ("c4", 4),
        ("c3", 3),
        ("c2", 2),
        ("c1", 1),
    ]
    assert history_repo.sha["s1"] not in {sha for sha, _, _ in writer.calls}
    assert writer.finished_with is True
    assert result.commits == 9


def test_out_of_scope_commits_still_reach_the_writer(history_repo, test_repo_id):
    writer = RecordingWriter()
    _walk(history_repo, test_repo_id, writer)
    resolved = {history_repo.name_of(sha): paths for sha, _, paths in writer.calls}
    assert resolved["c4"] == []  # README only: out of scope, but its row is still needed
    assert resolved["c1"] == ["core/src/pkg/alpha.py", "core/src/pkg/b.py"]  # today's names


def test_a_rejected_commit_is_dead_lettered_and_the_walk_goes_on(history_repo, test_repo_id, query):
    writer = RecordingWriter(reject=frozenset({history_repo.sha["c3"]}))
    result, _ = _walk(history_repo, test_repo_id, writer)
    assert len(writer.calls) == 9
    assert result.failures == 1
    assert query(
        "SELECT commit_sha, error_class FROM dead_letters WHERE repo_id = %s", test_repo_id
    ) == [(history_repo.sha["c3"], "ValueError")]


def test_any_other_writer_failure_stops_the_walk(history_repo, test_repo_id, query):
    writer = RecordingWriter(explode=frozenset({history_repo.sha["c6"]}))
    with pytest.raises(RuntimeError, match="went away"):
        _walk(history_repo, test_repo_id, writer)
    assert query("SELECT status::text FROM ingest_runs WHERE repo_id = %s", test_repo_id) == [
        ("failed",)
    ]


def test_a_truncated_walk_tells_the_writer(history_repo, test_repo_id):
    writer = RecordingWriter()
    _walk(history_repo, test_repo_id, writer, max_commits=3)
    assert [topo for _, topo, _ in writer.calls] == [9, 8, 7]
    assert writer.finished_with is False


def test_a_dropped_record_fails_the_walk(history_repo, test_repo_id, query, monkeypatch):
    real_stream = git_walk.raw_log_stream

    def drops_one(root: Any, *, rev: str = "HEAD") -> Any:
        records = list(real_stream(root, rev=rev))
        return iter(records[:4] + records[5:])

    monkeypatch.setattr(git_walk, "raw_log_stream", drops_one)
    with pytest.raises(IngestInvariantError, match="produced 8"):
        _walk(history_repo, test_repo_id, RecordingWriter())
    assert query("SELECT status::text FROM ingest_runs WHERE repo_id = %s", test_repo_id) == [
        ("failed",)
    ]

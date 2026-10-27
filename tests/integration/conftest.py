r"""Fixtures for tests that walk a real git repository into pace-db.

`history_repo` builds the same repository every time:

    main:  c1 -- c2 -- c3 -- c4 -- m5 -- c6 -- c7 -- c8 -- c9
                         \           /
    side:                 s1 -------

    c1  init                          add pkg/a.py, pkg/b.py, README.md
    c2  edit a (#11)                  modify pkg/a.py
    c3  rename a to alpha             pkg/a.py -> pkg/alpha.py
    s1  side: edit b                  modify pkg/b.py            (side branch only)
    c4  edit readme                   modify README.md           (out of scope)
    m5  Merge branch side (#12)       brings s1's pkg/b.py change in
    c6  move pkg into core/src (#13)  pkg/* -> core/src/pkg/*    (the 2025 move, small)
    c7  edit alpha after move (#14)   modify core/src/pkg/alpha.py
    c8  delete b                      delete core/src/pkg/b.py
    c9  re-add b                      add core/src/pkg/b.py with new content

The scope is today's two files. The first-parent walk is the nine `main` commits;
s1 is reachable only through m5's second parent -- the corpus's 1,031-merge case in
miniature.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ALPHA_V1 = "def alpha():\n    return 1\n\n\ndef helper():\n    return 'one'\n"
ALPHA_V2 = "def alpha():\n    return 2\n\n\ndef helper():\n    return 'one'\n"
ALPHA_V3 = "def alpha():\n    return 3\n\n\ndef helper():\n    return 'three'\n"
B_V1 = "class B:\n    size = 1\n\n    def grow(self):\n        self.size += 1\n"
B_V2 = "class B:\n    size = 2\n\n    def grow(self):\n        self.size += 1\n"
B_V3 = "VALUES = [1, 2, 3]\nTOTAL = sum(VALUES)\n"


@dataclass(frozen=True)
class HistoryRepo:
    root: Path
    sha: dict[str, str]
    scope: list[str]

    def name_of(self, sha: str) -> str:
        return {value: key for key, value in self.sha.items()}[sha]

    def blob(self, rev: str, path: str) -> str:
        return _git(self.root, "rev-parse", f"{rev}:{path}")


def _git(root: Path, *args: str, day: int | None = None) -> str:
    env = dict(os.environ)
    if day is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"2020-01-{day:02d}T12:00:00+00:00"
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, env=env
    )
    return done.stdout.strip()


@pytest.fixture
def hermetic_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """No global or system git config: the developer's signing key and aliases stay out."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Fixture Author")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.com")


@pytest.fixture
def history_repo(tmp_path: Path, hermetic_git: None) -> HistoryRepo:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    sha: dict[str, str] = {}
    day = 0

    def write(rel: str, text: str) -> None:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(name: str, message: str) -> None:
        nonlocal day
        day += 1
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", message, day=day)
        sha[name] = _git(root, "rev-parse", "HEAD")

    write("pkg/a.py", ALPHA_V1)
    write("pkg/b.py", B_V1)
    write("README.md", "fixture\n")
    commit("c1", "init")
    write("pkg/a.py", ALPHA_V2)
    commit("c2", "edit a (#11)")
    _git(root, "mv", "pkg/a.py", "pkg/alpha.py")
    commit("c3", "rename a to alpha")
    _git(root, "switch", "-q", "-c", "side")
    write("pkg/b.py", B_V2)
    commit("s1", "side: edit b")
    _git(root, "switch", "-q", "main")
    write("README.md", "fixture, edited\n")
    commit("c4", "edit readme")
    day += 1
    _git(root, "merge", "-q", "--no-ff", "side", "-m", "Merge branch side (#12)", day=day)
    sha["m5"] = _git(root, "rev-parse", "HEAD")
    (root / "core" / "src").mkdir(parents=True)
    _git(root, "mv", "pkg", "core/src/pkg")
    commit("c6", "move pkg into core/src (#13)")
    write("core/src/pkg/alpha.py", ALPHA_V3)
    commit("c7", "edit alpha after move (#14)")
    _git(root, "rm", "-q", "core/src/pkg/b.py")
    commit("c8", "delete b")
    write("core/src/pkg/b.py", B_V3)
    commit("c9", "re-add b")
    return HistoryRepo(root=root, sha=sha, scope=["core/src/pkg/alpha.py", "core/src/pkg/b.py"])


@pytest.fixture
def test_repo_id(migrated_database: Any) -> Iterator[int]:
    """A throwaway repositories row. Deleting it cascades to everything a test wrote."""
    row = migrated_database.execute(
        "INSERT INTO repositories (owner, name) VALUES ('pace-test', %s) RETURNING repo_id",
        (f"fixture-{uuid.uuid4().hex[:12]}",),
    ).fetchone()
    repo_id = int(row[0])
    yield repo_id
    migrated_database.execute("DELETE FROM repositories WHERE repo_id = %s", (repo_id,))


@pytest.fixture
def query(migrated_database: Any) -> Callable[..., list[tuple[Any, ...]]]:
    def run(sql: str, *params: Any) -> list[tuple[Any, ...]]:
        return list(migrated_database.execute(sql, params).fetchall())

    return run


@pytest.fixture
def writer_conn(migrated_database: Any) -> Iterator[Any]:
    """A NON-autocommit connection, as GraphWriter requires (savepoints need a transaction)."""
    import psycopg

    from provenance.config import settings

    with psycopg.connect(settings.database_url) as conn:
        yield conn


@pytest.fixture
def ingest(history_repo: HistoryRepo, test_repo_id: int, writer_conn: Any) -> Callable[..., Any]:
    """Walk the fixture repository into the test repository row; returns the writer."""
    from provenance.ingest.git_walk import walk_history
    from provenance.ingest.graph_writer import GraphWriter

    def run(
        *, rev: str = "HEAD", max_commits: int | None = None, scope: list[str] | None = None
    ) -> Any:
        chosen = history_repo.scope if scope is None else scope
        writer = GraphWriter(writer_conn, repo_id=test_repo_id, batch_size=3)
        walk_history(
            history_repo.root,
            rev=rev,
            scope=chosen,
            repo_id=test_repo_id,
            writer=writer,
            checkpoint_every=3,
            max_commits=max_commits,
        )
        return writer

    return run

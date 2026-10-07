"""The scope file list is the denominator of every recall figure in the project.

Two invariants are worth locking, because both failed silently rather than loudly:

* **`api_fastapi` is out of scope.** ROADMAP §3 names four exclusions and
  `DEFAULT_EXCLUDE` carried three, so `pace scope` selected 480 files / 112,259 lines
  where the roadmap claims 303 / 81,619. Nothing failed — the wrong corpus simply
  became the denominator, dominated by exactly the low-rationale-density REST
  boilerplate the exclusion argument is about.

* **The first freeze is guarded.** `ScopeDriftError` only fires when a frozen file
  already exists, so the one freeze that cannot be compared against anything — the
  first — was the only unguarded one. A wrong glob set frozen as `v1` is invisible
  afterwards, because every later run agrees with it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from provenance.ingest.scope import (
    DEFAULT_EXCLUDE,
    DEFAULT_INCLUDE,
    EXPECTED_SCOPE_FILES,
    SCOPE_VERSION,
    ScopeDriftError,
    _corpus_commit,
    _write_frozen,
    build_scope,
    default_scope_path,
    load_scope,
    select_paths,
)

PKG = "airflow-core/src/airflow"


def _corpus(tmp_path: Path, *rel_paths: str) -> Path:
    for rel in rel_paths:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def test_api_fastapi_is_excluded(tmp_path: Path) -> None:
    corpus = _corpus(
        tmp_path,
        f"{PKG}/models/dag.py",
        f"{PKG}/api_fastapi/routes/public.py",
        f"{PKG}/api_fastapi/core_api/datamodels/dags.py",
    )
    selected = select_paths(corpus, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    assert selected == [f"{PKG}/models/dag.py"]


def test_the_other_three_documented_exclusions_still_hold(tmp_path: Path) -> None:
    """Guards against 'fixing' the glob list by replacing rather than appending."""
    corpus = _corpus(
        tmp_path,
        f"{PKG}/models/dag.py",
        f"{PKG}/ui/app.py",
        f"{PKG}/example_dags/tutorial.py",
        f"{PKG}/migrations/versions/0001_x.py",
    )
    selected = select_paths(corpus, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    assert selected == [f"{PKG}/models/dag.py"]


def test_first_freeze_refuses_an_unexpected_file_count(tmp_path: Path) -> None:
    target = tmp_path / "scope.txt"
    with pytest.raises(ScopeDriftError, match=str(EXPECTED_SCOPE_FILES)):
        _write_frozen(target, [f"{PKG}/models/dag.py"], force=False)
    assert not target.exists(), "refused freeze must not leave a partial file"


def test_first_freeze_accepts_the_expected_file_count(tmp_path: Path) -> None:
    target = tmp_path / "scope.txt"
    paths = [f"{PKG}/m{i}.py" for i in range(EXPECTED_SCOPE_FILES)]
    _write_frozen(target, paths, force=False)
    assert target.read_text(encoding="utf-8").splitlines() == paths


def test_force_bypasses_the_first_freeze_guard(tmp_path: Path) -> None:
    """The escape hatch for a deliberate re-scope, same as the drift guard's."""
    target = tmp_path / "scope.txt"
    _write_frozen(target, [f"{PKG}/models/dag.py"], force=True)
    assert target.read_text(encoding="utf-8") == f"{PKG}/models/dag.py\n"


def test_drift_against_an_existing_freeze_still_raises(tmp_path: Path) -> None:
    """The pre-existing guard must survive the new one."""
    target = tmp_path / "scope.txt"
    paths = [f"{PKG}/m{i}.py" for i in range(EXPECTED_SCOPE_FILES)]
    _write_frozen(target, paths, force=False)
    with pytest.raises(ScopeDriftError, match="frozen"):
        _write_frozen(target, paths[:-1] + [f"{PKG}/other.py"], force=False)


# ------------------------------------------------------------------ corpus commit
# A file list without the tree it was computed from is not reproducible: apache/airflow
# moves daily, so "303 files" names a different 303 next month. `GoldenRecord` already
# reserves a (null) corpus_commit; this populates the same idea one layer up.


def _git_corpus(tmp_path: Path) -> Path:
    run = lambda *a: subprocess.run(a, cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    run("git", "config", "commit.gpgsign", "false")
    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")
    return tmp_path


def test_corpus_commit_is_read_from_the_corpus(tmp_path: Path) -> None:
    corpus = _git_corpus(tmp_path)
    expected = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=corpus, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert _corpus_commit(corpus) == expected


def test_corpus_commit_is_none_outside_a_repo(tmp_path: Path) -> None:
    """A corpus that is not a git clone is a legitimate state, not a crash."""
    assert _corpus_commit(tmp_path) is None


def test_build_scope_records_the_corpus_commit(tmp_path: Path) -> None:
    corpus = _git_corpus(tmp_path)
    (corpus / PKG).mkdir(parents=True)
    (corpus / PKG / "dag.py").write_text("x = 1\n", encoding="utf-8")
    report = build_scope(corpus, write=False)
    assert report.corpus_commit == _corpus_commit(corpus)
    assert '"corpus_commit"' in report.to_json()


# ------------------------------------------------------- no scope, no walking skeleton


def test_demo_refuses_to_run_without_a_frozen_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing denominator is an error, not a `log.warning` and a different file.

    `_pick_path` used to fall back to "the first .py under the corpus", so with no
    committed scope.txt the 14-span demo trace was produced over an arbitrary file
    while still reading as a passing walking skeleton.
    """
    from provenance.ingest import skeleton

    (tmp_path / PKG).mkdir(parents=True)
    (tmp_path / PKG / "aaa_first_alphabetically.py").write_text("x = 1\n", encoding="utf-8")

    def _missing(*_a: object, **_k: object) -> list[str]:
        raise FileNotFoundError("scope.txt missing")

    monkeypatch.setattr(skeleton, "load_scope", _missing)
    with pytest.raises(FileNotFoundError):
        skeleton._pick_path(None)


def test_an_explicit_path_still_bypasses_the_scope() -> None:
    """`--path` is the documented escape hatch and must keep working."""
    from provenance.ingest import skeleton

    assert skeleton._pick_path(f"{PKG}/models/dag.py") == f"{PKG}/models/dag.py"


# --------------------------------------------------------- the committed artifact
# `pace scope --check` needs a corpus clone and a GitHub runner has none, so drift
# against the tree can only be checked where the corpus lives. What IS checkable with
# no corpus — and what was actually broken — is whether the frozen list exists at all
# and agrees with the number the write-up quotes.


def test_the_committed_scope_exists_and_matches_the_pinned_count() -> None:
    frozen = load_scope(default_scope_path())
    assert len(frozen) == EXPECTED_SCOPE_FILES
    assert all(rel.endswith(".py") for rel in frozen)
    assert not [rel for rel in frozen if "/api_fastapi/" in rel]


def test_the_committed_stats_agree_with_the_committed_list() -> None:
    stats = json.loads(
        (default_scope_path().with_name("scope_stats.json")).read_text(encoding="utf-8")
    )
    assert stats["files"] == EXPECTED_SCOPE_FILES == len(load_scope(default_scope_path()))
    assert stats["scope_version"] == SCOPE_VERSION
    assert stats["corpus_commit"], "a file list without the tree it came from is not reproducible"

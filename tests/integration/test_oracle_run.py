"""Slice C3b: `pace eval oracle` writes its report only when every check passes."""

from __future__ import annotations

import json
import subprocess

from provenance.eval.oracle import run_oracle
from provenance.eval.preregistration import FAILURE_WORDS


def _run(fixture, out, **changes):
    arguments = {
        "codeshovel": fixture.codeshovel,
        "clones": fixture.clones,
        "out": out,
        "root": fixture.root,
        "pinned": fixture.codeshovel_commit,
        "expected_files": 6,
    }
    arguments.update(changes)
    return run_oracle(**arguments)


def _head(path):
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _refused(fixture, tmp_path, capsys, reason, **changes):
    out = tmp_path / "oracle.json"
    assert _run(fixture, out, **changes) == 1
    assert not out.exists()
    err = capsys.readouterr().err
    assert "nothing written" in err
    assert reason in err


def test_the_run_writes_the_report_and_the_same_bytes_twice(oracle_repo, tmp_path):
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    assert _run(oracle_repo, first) == 0
    assert _run(oracle_repo, second) == 0
    assert first.read_bytes() == second.read_bytes()
    found = json.loads(first.read_text(encoding="utf-8"))
    inputs = found["inputs"]
    assert (
        inputs["repository_commit"] == inputs["preregistration_commit"] == _head(oracle_repo.root)
    )
    assert inputs["codeshovel_commit"] == oracle_repo.codeshovel_commit
    assert (inputs["oracle_files"], inputs["detector"]) == (6, "lineage-mvp-1")
    assert found["claim_3"]["verdict"] == FAILURE_WORDS["identity"]


def test_a_modified_tracked_file_writes_nothing(oracle_repo, tmp_path, capsys):
    (oracle_repo.root / "docs" / "PREREGISTRATION.md").write_text("# edited\n", encoding="utf-8")
    _refused(oracle_repo, tmp_path, capsys, "tracked files are modified")


def test_another_codeshovel_commit_writes_nothing(oracle_repo, tmp_path, capsys):
    _refused(oracle_repo, tmp_path, capsys, "not the pinned 0000000000", pinned="0" * 40)


def test_an_edited_oracle_file_writes_nothing(oracle_repo, tmp_path, capsys):
    path = oracle_repo.oracle_dir / "fixture-views-title.json"
    path.write_text(path.read_text(encoding="utf-8").replace("Yintroduced", "Ybodychange"))
    _refused(oracle_repo, tmp_path, capsys, "differ from CodeShovel's commit")


def test_a_wrong_number_of_oracle_files_writes_nothing(oracle_repo, tmp_path, capsys):
    _refused(oracle_repo, tmp_path, capsys, "6 oracle files, not 40", expected_files=40)


def test_a_label_missing_from_the_clone_writes_nothing(oracle_repo, tmp_path, capsys):
    pinned = oracle_repo.rewrite("fixture-views-title", expectedResult={"f" * 40: "Yintroduced"})
    _refused(oracle_repo, tmp_path, capsys, f"{'f' * 40} is not in fixture.git", pinned=pinned)


def test_a_label_off_the_start_commits_history_writes_nothing(oracle_repo, tmp_path, capsys):
    labels = {oracle_repo.sha["c1"]: "Yintroduced", oracle_repo.sha["s1"]: "Ybodychange"}
    pinned = oracle_repo.rewrite("fixture-views-title", expectedResult=labels)
    reason = f"{oracle_repo.sha['s1']} is not an ancestor of its start"
    _refused(oracle_repo, tmp_path, capsys, reason, pinned=pinned)


def test_a_function_that_does_not_resolve_writes_nothing(oracle_repo, tmp_path, capsys):
    pinned = oracle_repo.rewrite("fixture-views-dispatch", functionStartLine=2)
    reason = "no function definition starts at line 2"
    _refused(oracle_repo, tmp_path, capsys, reason, pinned=pinned)

"""Slice C2: the git layer on the fixture repository (tests/integration/blame_fixture.py).

No database: these run git only. The fixture holds the three cases ADR-0005's addendum of
2026-11-09 decides about: a side branch, a directory move and a mechanical commit.
"""

from __future__ import annotations

import subprocess

import pytest
from blame_fixture import OLD_PATH, PATH, SPANS

from provenance.ingest.blame import GitFailed, blame_lines, line_log, renames


def _by_line(owners):
    return {owner.line: owner for owner in owners}


def test_first_parent_blame_credits_the_merge_and_never_the_side_commit(blame_repo):
    owners = _by_line(blame_lines(blame_repo.root, "HEAD", PATH, SPANS["total"]))
    assert owners[9].commit == blame_repo.sha["m3"]
    assert blame_repo.sha["s1"] not in {owner.commit for owner in owners.values()}


def test_blame_keeps_the_path_each_line_was_written_at(blame_repo):
    owners = _by_line(blame_lines(blame_repo.root, "HEAD", PATH, SPANS["total"]))
    assert (owners[7].commit, owners[7].path) == (blame_repo.sha["c1"], OLD_PATH)
    assert (owners[8].commit, owners[8].path) == (blame_repo.sha["c6"], PATH)


def test_ignoring_a_commit_reattributes_what_it_changed_but_not_what_it_added(blame_repo, tmp_path):
    ignore = tmp_path / "mechanical-revs"
    ignore.write_text(blame_repo.sha["c5"] + "\n", encoding="ascii")
    spans = (*SPANS["total"], *SPANS["scale"])
    plain = _by_line(blame_lines(blame_repo.root, "HEAD", PATH, spans))
    kept = _by_line(blame_lines(blame_repo.root, "HEAD", PATH, spans, ignore_revs=ignore))
    c1, c5 = blame_repo.sha["c1"], blame_repo.sha["c5"]
    assert (plain[5].commit, kept[5].commit) == (c5, c1)  # `result = 0` -> `result: int = 0`
    assert (plain[14].commit, kept[14].commit) == (c5, c1)  # `return x * 2` -> `return (x * 2)`
    assert (plain[6].commit, kept[6].commit) == (c5, c5)  # added by c5, matching no line


def test_one_call_with_every_range_equals_one_call_per_range(blame_repo):
    spans = sorted(span for name in SPANS for span in SPANS[name])
    together = blame_lines(blame_repo.root, "HEAD", PATH, spans)
    apart = [
        owner for span in spans for owner in blame_lines(blame_repo.root, "HEAD", PATH, [span])
    ]
    assert together == apart


def test_line_log_follows_first_parents_and_the_move(blame_repo):
    sha = blame_repo.sha
    total = line_log(blame_repo.root, "HEAD", PATH, SPANS["total"])
    assert set(total) == {sha["c1"], sha["m3"], sha["c5"], sha["c6"]}
    assert set(line_log(blame_repo.root, "HEAD", PATH, SPANS["scale"])) == {sha["c1"], sha["c5"]}
    assert line_log(blame_repo.root, "HEAD", PATH, SPANS["Box.size"]) == [sha["c1"]]


def test_renames_are_read_against_the_first_parent(blame_repo):
    assert renames(blame_repo.root, blame_repo.sha["c4"]) == {PATH: OLD_PATH}


def test_a_configured_ignore_list_cannot_turn_plain_blame_into_filtered_blame(
    blame_repo, tmp_path, monkeypatch
):
    ignore = tmp_path / "mechanical-revs"
    ignore.write_text(blame_repo.sha["c5"] + "\n", encoding="ascii")
    user_config = tmp_path / "gitconfig"
    user_config.write_text(f"[blame]\n\tignoreRevsFile = {ignore}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user_config))
    subprocess.run(
        ["git", "-C", str(blame_repo.root), "config", "blame.ignoreRevsFile", str(ignore)],
        check=True,
    )
    owners = _by_line(blame_lines(blame_repo.root, "HEAD", PATH, SPANS["scale"]))
    assert owners[14].commit == blame_repo.sha["c5"]


def test_a_git_failure_carries_gits_own_words(blame_repo):
    with pytest.raises(GitFailed, match="no such path"):
        blame_lines(blame_repo.root, "HEAD", "src/pkg/missing.py", [(1, 1)])

"""Slice B4's rules: no fetch before the self-assessment, and nothing stored that §2 excludes."""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from provenance.ingest.github_backfill import (
    DATA_PROTECTION,
    EthicsGateClosed,
    check_ethics,
    is_bot,
    issue_comment_row,
    issue_row,
    manifest_line,
    pull_request_row,
    review_comment_row,
)
from provenance.privacy import EMAIL_RE

SHA = "ab" * 20
TODAY = date(2026, 10, 30)
PR = {
    "number": 11,
    "node_id": "PR_x",
    "title": "Fix alpha (ask ada@example.org)",
    "body": "Because <!-- hidden --> mail me: dev@example.org",
    "state": "closed",
    "draft": False,
    "user": {"login": "ada", "type": "User", "avatar_url": "https://avatars/1"},
    "author_association": "MEMBER",
    "created_at": "2024-01-02T03:04:05Z",
    "updated_at": "2024-01-03T03:04:05Z",
    "closed_at": "2024-01-03T03:04:05Z",
    "merged_at": "2024-01-03T03:04:05Z",
    "merge_commit_sha": SHA,
    "head": {"sha": SHA, "ref": "ada-patch-1"},
    "base": {"ref": "main"},
    "additions": 3,
    "deletions": 1,
    "changed_files": 1,
    "comments": 2,
    "review_comments": 150,
    "labels": [{"name": "area:core"}],
}


def test_no_completion_line_and_a_future_date_both_close_the_gate():
    with pytest.raises(EthicsGateClosed, match="no dated"):
        check_ethics("## 7. Ethics\n\nNot yet.\n", today=TODAY)
    with pytest.raises(EthicsGateClosed, match="after today"):
        check_ethics("**Self-assessment completed:** 2026-10-31, by me\n", today=TODAY)


def test_a_dated_self_assessment_opens_the_gate():
    text = "**Self-assessment completed:** 2026-10-29, by the researcher\n"
    assert check_ethics(text, today=TODAY) == date(2026, 10, 29)


def test_the_committed_statement_is_dated():
    text = DATA_PROTECTION.read_text(encoding="utf-8")
    assert check_ethics(text, today=TODAY) == date(2026, 10, 29)


def test_bots_are_what_github_says_they_are():
    assert is_bot({"login": "boring-cyborg[bot]", "type": "Bot"})
    assert not is_bot({"login": "renovate-ish", "type": "User"})
    assert not is_bot(None)


def test_a_pull_request_row_keeps_only_what_section_2_allows():
    row = pull_request_row(7, PR)
    assert row["state"] == "merged"  # GitHub says closed; merged_at says merged
    assert "@" not in row["title"] + row["body"]
    assert row["author_login"] == "ada" and row["author_is_bot"] is False
    assert row["labels"] == ["area:core"] and row["review_comment_count"] == 150
    assert not {"author_association", "head_ref", "raw", "avatar_url"} & set(row)


def test_the_issue_row_for_a_pr_carries_no_text_and_no_login():
    row = issue_row(7, PR)
    assert (row["issue_number"], row["state"], row["comment_count"]) == (11, "closed", 2)
    assert not {"title", "body", "author_login"} & set(row)


def test_comment_rows_are_scrubbed_and_anchored_on_the_original_diff():
    review = {
        "id": 99,
        "pull_request_review_id": 5,
        "in_reply_to_id": None,
        "user": {"login": "bob", "type": "User"},
        "body": "ping bob@example.net",
        "path": "pkg/a.py",
        "diff_hunk": "@@ -1 +1 @@\n-# carol@example.com",
        "position": 3,
        "original_position": 2,
        "original_line": 10,
        "original_start_line": None,
        "original_commit_id": SHA,
        "commit_id": SHA,
        "line": 11,
        "start_line": None,
        "side": "RIGHT",
        "start_side": None,
        "subject_type": "line",
        "created_at": "2024-01-02T03:04:05Z",
        "updated_at": "2024-01-02T03:04:05Z",
    }
    row = review_comment_row(7, 11, review)
    # The diff hunk's own "@@ -1 +1 @@" header is not an email; only the real address
    # in it must be scrubbed.
    assert EMAIL_RE.search(row["body"] + row["diff_hunk"]) is None
    assert "@@ -1 +1 @@" in row["diff_hunk"]
    assert (row["original_commit_sha"], row["original_line"], row["pr_number"]) == (SHA, 10, 11)
    talk = {"id": 5, "user": {"login": "x[bot]", "type": "Bot"}, "body": "hi", "created_at": "t"}
    assert issue_comment_row(7, 11, talk)["author_is_bot"] is True


def test_a_manifest_line_is_ids_and_a_hash_never_text():
    line = manifest_line("review_comment", 11, 99, "the scrubbed text")
    assert json.loads(line) == {
        "kind": "review_comment",
        "pr": 11,
        "id": 99,
        "sha256": hashlib.sha256(b"the scrubbed text").hexdigest(),
    }
    assert "scrubbed" not in line

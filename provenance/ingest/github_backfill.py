"""Slice B4: fetch the text of the pool's PRs, under a request budget and the ethics gate.

docs/tasks/eval-harness/004_github_backfill.md. For each of the PRs that slice B2 linked to
an in-scope commit: one request for the PR, then pages of its inline review comments and its
conversation comments only when their counts are above zero. Each PR is stored in one
transaction, as DATA-PROTECTION.md requires:

* every text field passes `scrub_emails()` before storage;
* from GitHub's `user` object only `login` and whether the account is a bot are kept;
* the raw response, `author_association` and `head_ref` are never stored.

Nothing runs before DATA-PROTECTION.md §7 records a dated self-assessment (finding 11).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Final

from provenance.privacy import scrub_emails

DRIVER: Final = "github_backfill"
STAGE: Final = "github_backfill"
DATA_PROTECTION: Final = Path(__file__).resolve().parents[2] / "docs" / "DATA-PROTECTION.md"
_SELF_ASSESSED_RE: Final = re.compile(
    r"^\*\*Self-assessment completed:\*\* (\d{4}-\d{2}-\d{2})", re.MULTILINE
)


class EthicsGateClosed(RuntimeError):
    """DATA-PROTECTION.md §7 has no dated self-assessment yet, or dates it in the future."""


def self_assessment_date(text: str) -> date | None:
    """The date on DATA-PROTECTION.md §7's completion line, or None."""
    match = _SELF_ASSESSED_RE.search(text)
    return None if match is None else date.fromisoformat(match.group(1))


def check_ethics(text: str, *, today: date) -> date:
    """The self-assessment's date, or EthicsGateClosed. Called before the first request."""
    completed = self_assessment_date(text)
    if completed is None:
        raise EthicsGateClosed(
            "DATA-PROTECTION.md §7 carries no dated self-assessment; nothing is fetched "
            "before one is committed (review finding 11)"
        )
    if completed > today:
        raise EthicsGateClosed(f"the self-assessment is dated {completed}, after today")
    return completed


def is_bot(user: Mapping[str, Any] | None) -> bool:
    """GitHub's own account type. A login rule (`...[bot]`) can miss a bot."""
    return user is not None and user.get("type") == "Bot"


def _login(user: Mapping[str, Any] | None) -> str | None:
    login = None if user is None else user.get("login")
    return None if login is None else str(login)


def _text(value: Any) -> str:
    return scrub_emails(value or "")


def pull_request_row(repo_id: int, pr: Mapping[str, Any]) -> dict[str, Any]:
    """`pull_requests` parameters. No raw response, association or head ref (§2)."""
    return {
        "repo_id": repo_id,
        "pr_number": int(pr["number"]),
        "node_id": pr.get("node_id"),
        "title": _text(pr.get("title")),
        "body": _text(pr.get("body")),
        "state": "merged" if pr.get("merged_at") else pr["state"],
        "is_draft": bool(pr.get("draft")),
        "author_login": _login(pr.get("user")),
        "author_is_bot": is_bot(pr.get("user")),
        "created_at": pr["created_at"],
        "updated_at": pr.get("updated_at"),
        "closed_at": pr.get("closed_at"),
        "merged_at": pr.get("merged_at"),
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "head_sha": (pr.get("head") or {}).get("sha"),
        "base_ref": (pr.get("base") or {}).get("ref"),
        "additions": int(pr.get("additions") or 0),
        "deletions": int(pr.get("deletions") or 0),
        "changed_files": int(pr.get("changed_files") or 0),
        "review_comment_count": int(pr.get("review_comments") or 0),
        "labels": [label["name"] for label in pr.get("labels") or []],
    }


def issue_row(repo_id: int, pr: Mapping[str, Any]) -> dict[str, Any]:
    """The `issues` row a PR's conversation comments reference. Its text lives in
    `pull_requests`, once; this row carries no text and no login."""
    return {
        "repo_id": repo_id,
        "issue_number": int(pr["number"]),
        "state": pr["state"],
        "comment_count": int(pr.get("comments") or 0),
        "created_at": pr["created_at"],
        "updated_at": pr.get("updated_at"),
        "closed_at": pr.get("closed_at"),
    }


def review_comment_row(repo_id: int, pr_number: int, c: Mapping[str, Any]) -> dict[str, Any]:
    """`review_comments` parameters, anchored on the original diff (migration 0003)."""
    return {
        "review_comment_id": int(c["id"]),
        "repo_id": repo_id,
        "pr_number": pr_number,
        "node_id": c.get("node_id"),
        "review_id": c.get("pull_request_review_id"),
        "in_reply_to_id": c.get("in_reply_to_id"),
        "author_login": _login(c.get("user")),
        "author_is_bot": is_bot(c.get("user")),
        "body": _text(c.get("body")),
        "path": c["path"],
        "diff_hunk": _text(c.get("diff_hunk")),
        "diff_position": c.get("position"),
        "original_position": c.get("original_position"),
        "original_line": c.get("original_line"),
        "original_start_line": c.get("original_start_line"),
        "original_commit_sha": c.get("original_commit_id"),
        "commit_sha": c.get("commit_id"),
        "line": c.get("line"),
        "start_line": c.get("start_line"),
        "side": c.get("side"),
        "start_side": c.get("start_side"),
        "subject_type": c.get("subject_type"),
        "created_at": c["created_at"],
        "updated_at": c.get("updated_at"),
    }


def issue_comment_row(repo_id: int, pr_number: int, c: Mapping[str, Any]) -> dict[str, Any]:
    """`issue_comments` parameters for one conversation comment on a PR."""
    return {
        "comment_id": int(c["id"]),
        "repo_id": repo_id,
        "issue_number": pr_number,
        "node_id": c.get("node_id"),
        "author_login": _login(c.get("user")),
        "author_is_bot": is_bot(c.get("user")),
        "body": _text(c.get("body")),
        "created_at": c["created_at"],
        "updated_at": c.get("updated_at"),
    }


def manifest_line(kind: str, pr: int, artifact_id: int | None, text: str) -> str:
    """One line of results/github_manifest.jsonl: identifiers and a hash, never text."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return json.dumps({"id": artifact_id, "kind": kind, "pr": pr, "sha256": digest}, sort_keys=True)

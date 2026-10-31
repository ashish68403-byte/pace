"""The fake GitHub that slice B4's tests and the gold-validity audit's tests share.

It serves history_repo's four linked PRs: #11 merged at c2, with 150 inline review comments
over two pages and 3 conversation comments (one by a bot, one blank; email addresses in the
description and a comment); #12 answers 404; #13 merged at a commit outside the graph; #14
merged at c7, with one conversation comment.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import SecretStr

from provenance.ingest.git_walk import STATUS_SUCCEEDED, Checkpointer
from provenance.ingest.github_backfill import DRIVER, backfill
from provenance.ingest.github_client import GitHubClient

PLENTY = {"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "9999999999"}
ROUTE = re.compile(r"/repos/[^/]+/[^/]+/(pulls|issues)/(\d+)(/comments)?$")
WHEN = "2020-01-02T00:00:00Z"


def _user(login: str, *, bot: bool = False) -> dict[str, Any]:
    return {"login": login, "type": "Bot" if bot else "User", "avatar_url": "https://a/1"}


def _pr(number: int, merge_sha: str, *, reviews: int = 0, talk: int = 0, body: str = "") -> Any:
    return {
        "number": number,
        "node_id": f"PR_{number}",
        "title": f"PR {number}",
        "body": body,
        "state": "closed",
        "draft": False,
        "user": _user("ada"),
        "author_association": "MEMBER",
        "created_at": WHEN,
        "updated_at": WHEN,
        "closed_at": WHEN,
        "merged_at": WHEN,
        "merge_commit_sha": merge_sha,
        "head": {"sha": merge_sha, "ref": "ada-patch-1"},
        "base": {"ref": "main"},
        "additions": 1,
        "deletions": 1,
        "changed_files": 1,
        "comments": talk,
        "review_comments": reviews,
        "labels": [{"name": "area:core"}],
    }


def _review(i: int, sha: str) -> dict[str, Any]:
    return {
        "id": 1000 + i,
        "pull_request_review_id": 1,
        "in_reply_to_id": None,
        "user": _user("bob"),
        "body": f"Why not {i}?",
        "path": "pkg/a.py",
        "diff_hunk": "@@ -1 +1 @@",
        "position": 1,
        "original_position": 1,
        "original_line": 1,
        "original_start_line": None,
        "original_commit_id": sha,
        "commit_id": sha,
        "line": 1,
        "start_line": None,
        "side": "RIGHT",
        "start_side": None,
        "subject_type": "line",
        "created_at": WHEN,
        "updated_at": WHEN,
    }


def _talk(cid: int, user: dict[str, Any], body: str) -> dict[str, Any]:
    return {"id": cid, "node_id": f"IC_{cid}", "user": user, "body": body, "created_at": WHEN}


def fake_github(sha: dict[str, str]) -> tuple[Any, list[str]]:
    """A handler serving the four PRs, and the list of paths it was asked for."""
    reviews = [_review(i, sha["c2"]) for i in range(150)]
    described = "Alpha must return 2.<!-- template: describe it --> Ping ada@example.org"
    prs = {
        11: _pr(11, sha["c2"], reviews=150, talk=3, body=described),
        13: _pr(13, "f" * 40),
        14: _pr(14, sha["c7"], talk=1, body="Helper says three now."),
    }
    talk = {
        11: [
            _talk(1, _user("carol"), "Agreed, two. carol@example.com"),
            _talk(2, _user("boring-cyborg[bot]", bot=True), "Congratulations on your first PR!"),
            _talk(3, _user("dave"), "   "),
        ],
        14: [_talk(4, _user("erin"), "Three, because the scheduler changed.")],
    }
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        match = ROUTE.search(request.url.path)
        assert match is not None, request.url.path
        kind, number, comments = match.group(1), int(match.group(2)), match.group(3)
        if number not in prs:
            return httpx.Response(404, json={"message": "Not Found"}, headers=PLENTY)
        if not comments:
            return httpx.Response(200, json=prs[number], headers=PLENTY)
        if kind == "issues":
            return httpx.Response(200, json=talk.get(number, []), headers=PLENTY)
        page = int(request.url.params.get("page", "1"))
        headers = dict(PLENTY)
        if page == 1:
            nxt = f"https://api.github.com{request.url.path}?per_page=100&page=2"
            headers["link"] = f'<{nxt}>; rel="next"'
        return httpx.Response(200, json=reviews[(page - 1) * 100 : page * 100], headers=headers)

    return handler, asked


def fetch(repo_id: int, conn: Any, handler: Any, *, max_requests: int = 20_000) -> Any:
    checkpointer = Checkpointer(repo_id=repo_id, driver=DRIVER, every=25, allow_spill=False)
    transport = httpx.MockTransport(handler)
    with GitHubClient(SecretStr("test-token"), transport=transport) as client:
        report = backfill(
            conn, repo_id, client, max_requests=max_requests, checkpointer=checkpointer
        )
    checkpointer.note(**report.as_checkpoint())
    checkpointer.finish(STATUS_SUCCEEDED)
    return report

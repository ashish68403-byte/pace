"""Slice B4 against pace-db: the backfill on history_repo, with a fake GitHub.

history_repo links four PRs (B2): #11 at c2, #12 at m5, #13 at c6, #14 at c7. The fake GitHub:
  #11  merged at c2; 150 inline review comments (two pages) and 3 conversation comments, one
       by a bot and one blank; email addresses in the description and in a comment
  #12  404: a number GitHub does not know as a PR
  #13  merged at a commit outside the graph; no comments
  #14  merged at c7; one conversation comment
One run therefore costs (1 + 2 + 1) + 1 + 1 + (1 + 1) = 8 requests.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from provenance.ingest.git_walk import STATUS_SUCCEEDED, Checkpointer
from provenance.ingest.github_backfill import DRIVER, backfill, plan, write_manifest
from provenance.ingest.github_client import GitHubClient
from provenance.ingest.message_pool import write_pool

pytestmark = pytest.mark.db

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


@pytest.fixture
def pooled(history_repo, test_repo_id, ingest, migrated_database):
    """The fixture ingested and its commit-message pool built: B2's four PR links."""
    ingest()
    write_pool(migrated_database, test_repo_id, numbering_starts=history_repo.sha["c1"])
    return history_repo


def test_one_run_costs_what_the_bound_says(pooled, test_repo_id, migrated_database, query):
    sha = pooled.sha
    handler, asked = fake_github(sha)
    report = fetch(test_repo_id, migrated_database, handler)
    assert (report.stats.requests, len(asked)) == (8, 8)
    assert (report.fetched, report.not_found) == (3, [12])
    prs = query("SELECT pr_number FROM pull_requests WHERE repo_id = %s ORDER BY 1", test_repo_id)
    assert prs == [(11,), (13,), (14,)]
    assert query("SELECT count(*) FROM review_comments WHERE repo_id = %s", test_repo_id) == [
        (150,)
    ]
    assert query("SELECT count(*) FROM issue_comments WHERE repo_id = %s", test_repo_id) == [(4,)]
    edges = query(
        "SELECT commit_sha, pr_number, source FROM edge_commit_pr WHERE repo_id = %s ORDER BY 2",
        test_repo_id,
    )
    assert edges == [(sha["c2"], 11, "merge_commit_sha"), (sha["c7"], 14, "merge_commit_sha")]
    dead = query(
        "SELECT subject_ref, error_class FROM dead_letters "
        "WHERE repo_id = %s AND stage = 'github_backfill'",
        test_repo_id,
    )
    assert dead == [("pull/12", "NotFound")]


def test_nothing_section_2_excludes_is_stored(pooled, test_repo_id, migrated_database, query):
    fetch(test_repo_id, migrated_database, fake_github(pooled.sha)[0])
    [(body, raw, association, head_ref)] = query(
        "SELECT body, raw, author_association, head_ref FROM pull_requests "
        "WHERE repo_id = %s AND pr_number = 11",
        test_repo_id,
    )
    assert "@" not in body and raw == {} and association is None and head_ref is None
    talk = query("SELECT body FROM issue_comments WHERE repo_id = %s", test_repo_id)
    assert all("@" not in text for (text,) in talk)
    bots = query(
        "SELECT comment_id FROM issue_comments WHERE repo_id = %s AND author_is_bot", test_repo_id
    )
    assert bots == [(2,)]
    issues = query(
        "SELECT issue_number, title, body, author_login, is_pull_request FROM issues "
        "WHERE repo_id = %s ORDER BY 1",
        test_repo_id,
    )
    assert issues == [(n, "", "", None, True) for n in (11, 13, 14)]


def test_a_second_run_asks_nothing_and_the_manifest_is_unchanged(
    pooled, test_repo_id, migrated_database, tmp_path
):
    fetch(test_repo_id, migrated_database, fake_github(pooled.sha)[0])
    lines = write_manifest(migrated_database, test_repo_id, tmp_path / "1.jsonl")
    handler, asked = fake_github(pooled.sha)
    second = fetch(test_repo_id, migrated_database, handler)
    write_manifest(migrated_database, test_repo_id, tmp_path / "2.jsonl")
    assert (second.stats.requests, asked) == (0, [])
    assert lines == 3 + 150 + 4  # descriptions, inline review comments, conversation
    assert (tmp_path / "1.jsonl").read_bytes() == (tmp_path / "2.jsonl").read_bytes()
    assert "Alpha" not in (tmp_path / "1.jsonl").read_text(encoding="utf-8")


def test_the_budget_stops_between_prs_never_inside_one(
    pooled, test_repo_id, migrated_database, query
):
    report = fetch(test_repo_id, migrated_database, fake_github(pooled.sha)[0], max_requests=4)
    assert (report.stopped_by_budget, report.fetched) == (True, 1)
    assert query("SELECT count(*) FROM review_comments WHERE repo_id = %s", test_repo_id) == [
        (150,)
    ]


def test_the_plan_makes_no_request(pooled, test_repo_id, migrated_database):
    todo, report = plan(migrated_database, test_repo_id)
    assert (todo, report.linked, report.already_stored) == ([11, 12, 13, 14], 4, 0)

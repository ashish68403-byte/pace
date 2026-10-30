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

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final

from pydantic import SecretStr

from provenance.config import settings
from provenance.graph.tables import (
    CHUNK_EVIDENCE,
    CHUNKS,
    COMMITS,
    DEAD_LETTERS,
    EDGE_COMMIT_PR,
    ISSUE_COMMENTS,
    ISSUES,
    PULL_REQUESTS,
    REPOSITORIES,
    REVIEW_COMMENTS,
)
from provenance.ingest.git_walk import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    Checkpointer,
    ConcurrentRunError,
)
from provenance.ingest.github_client import GitHubClient, GitHubError, NotFound, RequestStats
from provenance.privacy import scrub_emails

DRIVER: Final = "github_backfill"
STAGE: Final = "github_backfill"
DATA_PROTECTION: Final = Path(__file__).resolve().parents[2] / "docs" / "DATA-PROTECTION.md"
_SELF_ASSESSED_RE: Final = re.compile(
    r"^\*\*Self-assessment completed:\*\* (\d{4}-\d{2}-\d{2})", re.MULTILINE
)
DEFAULT_MAX_REQUESTS: Final = 20_000
DEFAULT_MANIFEST: Final = Path("results") / "github_manifest.jsonl"
#: How a dead letter names a number GitHub does not know as a PR; later runs skip it.
NOT_FOUND_ERROR: Final = NotFound.__name__

_LINKED_PRS_SQL: Final = f"""
    SELECT DISTINCT e.evidence_key::int
    FROM {CHUNK_EVIDENCE} e JOIN {CHUNKS} d USING (chunk_id)
    WHERE e.repo_id = %s AND d.kind = 'commit'
      AND e.source = 'linked' AND e.evidence_kind = 'pull_request'
    ORDER BY 1
"""
_STORED_PRS_SQL: Final = f"SELECT pr_number FROM {PULL_REQUESTS} WHERE repo_id = %s"
_NOT_FOUND_SQL: Final = f"""
    SELECT subject_ref FROM {DEAD_LETTERS}
    WHERE repo_id = %s AND stage = %s AND error_class = %s
"""
_SLUG_SQL: Final = f"SELECT owner || '/' || name FROM {REPOSITORIES} WHERE repo_id = %s"
_INSERT_PR: Final = f"""
    INSERT INTO {PULL_REQUESTS} (
        repo_id, pr_number, node_id, title, body, state, is_draft, author_login,
        author_is_bot, created_at, updated_at, closed_at, merged_at, merge_commit_sha,
        head_sha, base_ref, additions, deletions, changed_files, review_comment_count, labels
    ) VALUES (
        %(repo_id)s, %(pr_number)s, %(node_id)s, %(title)s, %(body)s, %(state)s, %(is_draft)s,
        %(author_login)s, %(author_is_bot)s, %(created_at)s, %(updated_at)s, %(closed_at)s,
        %(merged_at)s, %(merge_commit_sha)s, %(head_sha)s, %(base_ref)s, %(additions)s,
        %(deletions)s, %(changed_files)s, %(review_comment_count)s, %(labels)s
    )
    ON CONFLICT (repo_id, pr_number) DO NOTHING
"""
_INSERT_ISSUE: Final = f"""
    INSERT INTO {ISSUES} (
        repo_id, issue_number, state, comment_count, created_at, updated_at, closed_at,
        is_pull_request
    ) VALUES (
        %(repo_id)s, %(issue_number)s, %(state)s, %(comment_count)s, %(created_at)s,
        %(updated_at)s, %(closed_at)s, true
    )
    ON CONFLICT (repo_id, issue_number) DO NOTHING
"""
_INSERT_REVIEW: Final = f"""
    INSERT INTO {REVIEW_COMMENTS} (
        review_comment_id, repo_id, pr_number, node_id, review_id, in_reply_to_id,
        author_login, author_is_bot, body, path, diff_hunk, diff_position, original_position,
        original_line, original_start_line, original_commit_sha, commit_sha, line,
        start_line, side, start_side, subject_type, created_at, updated_at
    ) VALUES (
        %(review_comment_id)s, %(repo_id)s, %(pr_number)s, %(node_id)s, %(review_id)s,
        %(in_reply_to_id)s, %(author_login)s, %(author_is_bot)s, %(body)s, %(path)s,
        %(diff_hunk)s, %(diff_position)s, %(original_position)s, %(original_line)s,
        %(original_start_line)s, %(original_commit_sha)s, %(commit_sha)s, %(line)s,
        %(start_line)s, %(side)s, %(start_side)s, %(subject_type)s, %(created_at)s,
        %(updated_at)s
    )
    ON CONFLICT (review_comment_id) DO NOTHING
"""
_INSERT_CONVERSATION: Final = f"""
    INSERT INTO {ISSUE_COMMENTS} (
        comment_id, repo_id, issue_number, node_id, author_login, author_is_bot, body,
        created_at, updated_at
    ) VALUES (
        %(comment_id)s, %(repo_id)s, %(issue_number)s, %(node_id)s, %(author_login)s,
        %(author_is_bot)s, %(body)s, %(created_at)s, %(updated_at)s
    )
    ON CONFLICT (comment_id) DO NOTHING
"""
#: GitHub's own record of which commit landed the PR, kept only when that commit is ours.
_INSERT_EDGE: Final = f"""
    INSERT INTO {EDGE_COMMIT_PR} (repo_id, commit_sha, pr_number, source)
    SELECT c.repo_id, c.commit_sha, %(pr)s, 'merge_commit_sha'
    FROM {COMMITS} c WHERE c.repo_id = %(repo_id)s AND c.commit_sha = %(sha)s
    ON CONFLICT DO NOTHING
"""
_MANIFEST_SQL: Final = {
    "pull_request": f"SELECT pr_number, NULL, title, body FROM {PULL_REQUESTS} WHERE repo_id = %s",
    "review_comment": (
        f"SELECT pr_number, review_comment_id, NULL, body FROM {REVIEW_COMMENTS} WHERE repo_id = %s"
    ),
    "issue_comment": (
        f"SELECT issue_number, comment_id, NULL, body FROM {ISSUE_COMMENTS} WHERE repo_id = %s"
    ),
}


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


@dataclass
class BackfillReport:
    """One run: what it found, fetched and spent."""

    linked: int = 0
    already_stored: int = 0
    known_not_found: int = 0
    fetched: int = 0
    not_found: list[int] = field(default_factory=list)
    review_comments: int = 0
    conversation_comments: int = 0
    stopped_by_budget: bool = False
    stats: RequestStats = field(default_factory=RequestStats)

    def as_checkpoint(self) -> dict[str, Any]:
        """The run record's `checkpoint`: the census sums `requests` across runs."""
        return {
            "prs_fetched": self.fetched,
            "prs_not_found": len(self.not_found),
            "review_comments": self.review_comments,
            "conversation_comments": self.conversation_comments,
            "requests": self.stats.requests,
            "rate_limit_waits": self.stats.rate_limit_waits,
            "retries": self.stats.retries,
            "seconds_waited": round(self.stats.seconds_waited, 1),
            "stopped_by_budget": self.stopped_by_budget,
        }


def plan(conn: Any, repo_id: int) -> tuple[list[int], BackfillReport]:
    """The PRs left to fetch, in number order. Makes no request."""
    linked = [int(n) for (n,) in conn.execute(_LINKED_PRS_SQL, (repo_id,)).fetchall()]
    stored = {int(n) for (n,) in conn.execute(_STORED_PRS_SQL, (repo_id,)).fetchall()}
    refs = conn.execute(_NOT_FOUND_SQL, (repo_id, STAGE, NOT_FOUND_ERROR)).fetchall()
    gone = {int(ref.rsplit("/", 1)[1]) for (ref,) in refs}
    todo = [n for n in linked if n not in stored and n not in gone]
    report = BackfillReport(
        linked=len(linked),
        already_stored=len(stored.intersection(linked)),
        known_not_found=len(gone.intersection(linked)),
    )
    return todo, report


def store_pr(
    conn: Any,
    repo_id: int,
    pr: Mapping[str, Any],
    reviews: Sequence[Mapping[str, Any]],
    talk: Sequence[Mapping[str, Any]],
) -> None:
    """One PR, whole or not at all."""
    number = int(pr["number"])
    with conn.transaction():
        conn.execute(_INSERT_PR, pull_request_row(repo_id, pr))
        conn.execute(_INSERT_ISSUE, issue_row(repo_id, pr))
        with conn.cursor() as cur:
            if reviews:
                rows = [review_comment_row(repo_id, number, c) for c in reviews]
                cur.executemany(_INSERT_REVIEW, rows)
            if talk:
                cur.executemany(
                    _INSERT_CONVERSATION, [issue_comment_row(repo_id, number, c) for c in talk]
                )
        if pr.get("merged_at") and pr.get("merge_commit_sha"):
            conn.execute(
                _INSERT_EDGE, {"repo_id": repo_id, "sha": pr["merge_commit_sha"], "pr": number}
            )


def backfill(
    conn: Any,
    repo_id: int,
    client: GitHubClient,
    *,
    max_requests: int,
    checkpointer: Checkpointer,
) -> BackfillReport:
    """Fetch and store every PR left, stopping between PRs once the budget is spent."""
    todo, report = plan(conn, repo_id)
    report.stats = client.stats
    (slug,) = conn.execute(_SLUG_SQL, (repo_id,)).fetchone()
    for number in todo:
        if client.stats.requests >= max_requests:
            report.stopped_by_budget = True
            break
        try:
            pr = client.get_json(f"/repos/{slug}/pulls/{number}")
        except NotFound as exc:
            checkpointer.dead_letter(STAGE, f"pull/{number}", exc, status=exc.status)
            report.not_found.append(number)
            continue
        reviews = _pages(
            client, f"/repos/{slug}/pulls/{number}/comments", pr.get("review_comments")
        )
        talk = _pages(client, f"/repos/{slug}/issues/{number}/comments", pr.get("comments"))
        store_pr(conn, repo_id, pr, reviews, talk)
        report.fetched += 1
        report.review_comments += len(reviews)
        report.conversation_comments += len(talk)
        checkpointer.advance(**report.as_checkpoint())
    return report


def _pages(client: GitHubClient, path: str, count: Any) -> list[Any]:
    """A comment list costs nothing when GitHub counts none."""
    return client.get_pages(path) if count else []


def write_manifest(conn: Any, repo_id: int, path: Path) -> int:
    """results/github_manifest.jsonl from the database: every stored artifact, sorted."""
    entries: list[tuple[str, int, int, str]] = []
    for kind, sql in _MANIFEST_SQL.items():
        for pr, artifact_id, title, body in conn.execute(sql, (repo_id,)).fetchall():
            text = body if title is None else f"{title}\n\n{body}"
            line = manifest_line(kind, int(pr), artifact_id, text)
            entries.append((kind, int(pr), -1 if artifact_id is None else int(artifact_id), line))
    entries.sort()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for *_, line in entries), encoding="utf-8")
    return len(entries)


def render_plan(todo: Sequence[int], report: BackfillReport) -> str:
    return "\n".join(
        [
            f"github backfill plan: {report.linked:,} PRs linked by the pool",
            f"  already stored {report.already_stored:,}; "
            f"known not to be PRs {report.known_not_found:,}",
            f"  to fetch {len(todo):,}: at least {len(todo):,} requests, plus one per page of "
            "100 comments of each kind (counts arrive with each PR's first request)",
        ]
    )


def render_report(report: BackfillReport, written: int, manifest: Path) -> str:
    stats = report.stats
    per_pr = stats.requests / report.fetched if report.fetched else 0.0
    lines = [
        f"github backfill: {report.fetched:,} PRs fetched, {report.already_stored:,} already "
        f"stored, {len(report.not_found):,} not PRs on GitHub",
        f"  comments: {report.review_comments:,} inline review, "
        f"{report.conversation_comments:,} conversation",
        f"  requests: {stats.requests:,} ({per_pr:.2f} per PR fetched); "
        f"{stats.rate_limit_waits:,} rate-limit waits, {stats.retries:,} retries, "
        f"{stats.seconds_waited:,.0f} s waited",
        f"  wrote {written:,} lines to {manifest}",
    ]
    if report.stopped_by_budget:
        lines.append("  STOPPED at --max-requests; run the command again to continue")
    return "\n".join(lines)


def _run(
    conn: Any, repo_id: int, token: SecretStr, max_requests: int, assessed: date
) -> BackfillReport:
    checkpointer = Checkpointer(repo_id=repo_id, driver=DRIVER, every=25, allow_spill=False)
    checkpointer.note(self_assessment_completed=assessed.isoformat())
    with GitHubClient(token) as client:
        try:
            report = backfill(
                conn, repo_id, client, max_requests=max_requests, checkpointer=checkpointer
            )
        except BaseException as exc:
            checkpointer.note(requests=client.stats.requests)
            status = STATUS_CANCELLED if isinstance(exc, KeyboardInterrupt) else STATUS_FAILED
            checkpointer.finish(status, error=exc)
            raise
    checkpointer.note(**report.as_checkpoint())
    checkpointer.finish(STATUS_SUCCEEDED)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """`pace github backfill`: fetch the pool's PR text; resumable; budgeted."""
    parser = argparse.ArgumentParser(
        prog="pace github backfill",
        description="Fetch each linked PR's description and comments (slice B4).",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=DEFAULT_MAX_REQUESTS,
        help="stop between PRs once this many requests are spent",
    )
    parser.add_argument(
        "--plan", action="store_true", help="print what is left and its minimum cost; no request"
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="manifest path")
    args = parser.parse_args(list(argv) if argv is not None else None)

    from provenance.graph.db import connection
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    try:
        # Autocommit, so each PR's transaction commits on its own: a crash keeps every
        # PR stored before it, and the next run resumes from there.
        with connection(autocommit=True) as conn:
            repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
            todo, report = plan(conn, repo_id)
            if args.plan:
                print(render_plan(todo, report))
                return 0
            assessed = check_ethics(DATA_PROTECTION.read_text(encoding="utf-8"), today=date.today())
            if settings.github_token is None:
                missing = "not fetching: PACE_GITHUB_TOKEN is not set (docs/RUNBOOK.md)"
                print(missing, file=sys.stderr)
                return 2
            report = _run(conn, repo_id, settings.github_token, args.max_requests, assessed)
            written = write_manifest(conn, repo_id, Path(args.manifest))
    except (UnknownRepository, EthicsGateClosed, ConcurrentRunError) as exc:
        print(f"not fetching: {exc}", file=sys.stderr)
        return 2
    except GitHubError as exc:
        print(f"FETCH STOPPED: {exc}. What was fetched is stored; run again.", file=sys.stderr)
        return 1
    print(render_report(report, written, Path(args.manifest)))
    return 0

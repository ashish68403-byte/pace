#!/usr/bin/env python
"""Licence-compliant corpus acquisition for PACE.

WHAT FAILURE THIS PREVENTS
--------------------------
Two distinct ones, and they are both the kind that ends a project rather than delaying it.

1. COPYRIGHT. apache/airflow's *code* is Apache-2.0. The discussion around it -- issue
   bodies, PR descriptions, review comments -- is NOT. Each comment is the copyright of
   the person who wrote it, licensed to GitHub under their Terms of Service, not to us
   under Apache-2.0. Committing a dump of that text to a public repository redistributes
   several thousand people's writing without a licence to do it.

   So this script commits IDENTIFIERS ONLY:
       (repo, pr_number, issue_number, comment_id, commit_sha, sha256_of_body, url)
   Bodies are fetched at runtime into a gitignored cache. The manifest is a pointer, not
   a copy: reproducible (the sha256 proves you got the same bytes we did) without being
   a redistribution. GitHub's acceptable-use policy also permits API collection for
   research on the condition that resulting publications are open access -- this project
   is submitted as an open-access dissertation, which is what makes that condition met.

2. PERSONAL DATA. Commit metadata carries author email addresses. Logins are public
   pseudonymous handles that people chose to publish; email addresses are not, and there
   is no research justification for keeping them when the login already identifies the
   contributor for citation purposes. Emails are therefore hashed at ingestion and
   scrubbed out of body text before anything is written to disk. See docs/DATA-PROTECTION.md.

Usage
-----
    export GITHUB_TOKEN=ghp_...                 # a read-only classic/fine-grained token
    python scripts/fetch_corpus.py manifest --since 2023-01-01 --limit 5000
    python scripts/fetch_corpus.py hydrate      # fills the gitignored body cache
    python scripts/fetch_corpus.py verify       # re-checks every cached body's sha256
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from provenance.config import settings

GITHUB_API = "https://api.github.com"
USER_AGENT = "pace-research-corpus/0.1 (final-year project; open-access output)"
MANIFEST_VERSION = "v1"

# Deliberately broad. A false positive costs us a redacted token in a comment body; a
# false negative puts someone's email address in a corpus we then index and query.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


# ------------------------------------------------------------------ personal data


def hash_email(email: str) -> str:
    """One-way, stable, and NOT reversible without the original address.

    Stable so that "same author across commits" is still a queryable join key; hashed so
    that the address itself never lands in the database, the manifest, or a backup.
    Lowercased first because git records mixed case and the same human would otherwise
    hash to two different identities.
    """
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:32]


def scrub_emails(text: str) -> str:
    """Replace every email address in free text with a stable opaque marker."""
    return _EMAIL_RE.sub(lambda m: f"<email:{hash_email(m.group(0))[:12]}>", text)


def body_sha256(text: str) -> str:
    """Hash of the body as we received it, after email scrubbing.

    Scrub-then-hash, not hash-then-scrub: the hash must describe the bytes we actually
    keep, or `verify` can never succeed against a cache that has been scrubbed.
    """
    return hashlib.sha256(scrub_emails(text).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------- records


@dataclass(frozen=True)
class ManifestRecord:
    """IDs only. No body text ever enters this dataclass."""

    repo: str
    kind: str  # "issue" | "pull_request" | "issue_comment" | "review_comment" | "commit"
    url: str
    sha256_of_body: str
    pr_number: int | None = None
    issue_number: int | None = None
    comment_id: int | None = None
    commit_sha: str | None = None
    author_login: str | None = None  # public handle: retained, it is how we cite
    author_email_sha256: str | None = None  # hashed at ingestion, never the address
    created_at: str | None = None

    def key(self) -> str:
        """Stable cache key. Must be filesystem-safe and collision-free."""
        parts = [
            self.kind,
            str(self.pr_number or ""),
            str(self.issue_number or ""),
            str(self.comment_id or ""),
            self.commit_sha or "",
        ]
        return f"{self.kind}-" + hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:24]


# ------------------------------------------------------------------ github client


class GitHub:
    """Thin GitHub REST client: pagination, rate limits, and nothing else."""

    def __init__(self, token: str | None = None, timeout: float = 30.0) -> None:
        token = token or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise SystemExit(
                "GITHUB_TOKEN is not set. Unauthenticated GitHub gives 60 requests/hour, "
                "which will not complete a corpus run. Use a read-only token."
            )
        self.client = httpx.Client(
            base_url=GITHUB_API,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
            },
        )

    def close(self) -> None:
        self.client.close()

    def get(self, path: str, **params: Any) -> httpx.Response:
        for attempt in range(6):
            resp = self.client.get(path, params=params)
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                self._sleep_until_reset(resp)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                # Secondary rate limits and transient 5xx: back off, do not hammer.
                delay = float(resp.headers.get("retry-after", 2**attempt))
                print(f"  [{resp.status_code}] backing off {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp
        raise SystemExit(f"giving up on {path} after repeated rate limiting")

    @staticmethod
    def _sleep_until_reset(resp: httpx.Response) -> None:
        reset = int(resp.headers.get("x-ratelimit-reset", "0"))
        wait = max(reset - int(time.time()), 1) + 2
        print(f"  primary rate limit hit; sleeping {wait}s", file=sys.stderr)
        time.sleep(wait)

    def paginate(self, path: str, limit: int | None = None, **params: Any) -> Iterator[dict]:
        params.setdefault("per_page", 100)
        page = 1
        yielded = 0
        while True:
            resp = self.get(path, page=page, **params)
            batch = resp.json()
            if isinstance(batch, dict):  # search endpoints wrap results
                batch = batch.get("items", [])
            if not batch:
                return
            for item in batch:
                yield item
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if "next" not in (resp.headers.get("link") or ""):
                return
            page += 1


# --------------------------------------------------------------------- collectors


def collect_issues_and_prs(
    gh: GitHub, repo: str, since: str | None, limit: int | None
) -> Iterator[ManifestRecord]:
    owner, name = repo.split("/", 1)
    params: dict[str, Any] = {"state": "all", "sort": "updated", "direction": "desc"}
    if since:
        params["since"] = since
    for item in gh.paginate(f"/repos/{owner}/{name}/issues", limit=limit, **params):
        is_pr = "pull_request" in item
        body = item.get("body") or ""
        yield ManifestRecord(
            repo=repo,
            kind="pull_request" if is_pr else "issue",
            url=item["html_url"],
            sha256_of_body=body_sha256(body),
            pr_number=item["number"] if is_pr else None,
            issue_number=None if is_pr else item["number"],
            author_login=(item.get("user") or {}).get("login"),
            created_at=item.get("created_at"),
        )


def collect_issue_comments(
    gh: GitHub, repo: str, since: str | None, limit: int | None
) -> Iterator[ManifestRecord]:
    owner, name = repo.split("/", 1)
    params: dict[str, Any] = {"sort": "updated", "direction": "desc"}
    if since:
        params["since"] = since
    for c in gh.paginate(f"/repos/{owner}/{name}/issues/comments", limit=limit, **params):
        number = _number_from_issue_url(c.get("issue_url", ""))
        yield ManifestRecord(
            repo=repo,
            kind="issue_comment",
            url=c["html_url"],
            sha256_of_body=body_sha256(c.get("body") or ""),
            issue_number=number,
            comment_id=c["id"],
            author_login=(c.get("user") or {}).get("login"),
            created_at=c.get("created_at"),
        )


def collect_review_comments(
    gh: GitHub, repo: str, since: str | None, limit: int | None
) -> Iterator[ManifestRecord]:
    """Inline review comments -- the highest-value rationale source in the whole corpus.

    This is where "why is the code like this?" is actually answered: someone objected on
    a specific line and the author explained. Issue threads are mostly triage.
    """
    owner, name = repo.split("/", 1)
    params: dict[str, Any] = {"sort": "updated", "direction": "desc"}
    if since:
        params["since"] = since
    for c in gh.paginate(f"/repos/{owner}/{name}/pulls/comments", limit=limit, **params):
        yield ManifestRecord(
            repo=repo,
            kind="review_comment",
            url=c["html_url"],
            sha256_of_body=body_sha256(c.get("body") or ""),
            pr_number=_number_from_issue_url(c.get("pull_request_url", "")),
            comment_id=c["id"],
            author_login=(c.get("user") or {}).get("login"),
            created_at=c.get("created_at"),
        )


def collect_commits(
    gh: GitHub, repo: str, since: str | None, limit: int | None
) -> Iterator[ManifestRecord]:
    owner, name = repo.split("/", 1)
    params: dict[str, Any] = {}
    if since:
        params["since"] = since
    for c in gh.paginate(f"/repos/{owner}/{name}/commits", limit=limit, **params):
        commit = c.get("commit") or {}
        author = commit.get("author") or {}
        email = author.get("email")
        yield ManifestRecord(
            repo=repo,
            kind="commit",
            url=c["html_url"],
            sha256_of_body=body_sha256(commit.get("message") or ""),
            commit_sha=c["sha"],
            # Login retained (public, pseudonymous, needed for citation);
            # email hashed here and the raw value dropped on the floor immediately.
            author_login=(c.get("author") or {}).get("login"),
            author_email_sha256=hash_email(email) if email else None,
            created_at=author.get("date"),
        )


def _number_from_issue_url(url: str) -> int | None:
    m = re.search(r"/(?:issues|pulls)/(\d+)$", url)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------- commands


def cmd_manifest(args: argparse.Namespace) -> int:
    repo = f"{settings.repo_owner}/{settings.repo_name}"
    gh = GitHub(timeout=args.timeout)
    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)

    collectors = {
        "issues": collect_issues_and_prs,
        "issue_comments": collect_issue_comments,
        "review_comments": collect_review_comments,
        "commits": collect_commits,
    }
    selected = args.kinds or list(collectors)

    count = 0
    try:
        with out.open("w", encoding="utf-8") as fh:
            header = {
                "_manifest_version": MANIFEST_VERSION,
                "_repo": repo,
                "_collected_at": datetime.now(UTC).isoformat(),
                "_note": (
                    "IDs and body hashes only. Bodies are the authors' copyright and are "
                    "fetched at runtime; see docs/DATA-PROTECTION.md."
                ),
            }
            fh.write(json.dumps(header) + "\n")
            for kind in selected:
                print(f"collecting {kind} ...", file=sys.stderr)
                for rec in collectors[kind](gh, repo, args.since, args.limit):
                    fh.write(json.dumps(asdict(rec)) + "\n")
                    count += 1
                    if count % 500 == 0:
                        print(f"  {count} records", file=sys.stderr)
    finally:
        gh.close()

    print(f"wrote {count} ID-only records to {out}")
    return 0


def _cache_path(cache_dir: Path, rec: ManifestRecord) -> Path:
    key = rec.key()
    return cache_dir / key[:2] / f"{key}.txt"


def _fetch_body(gh: GitHub, rec: ManifestRecord) -> str:
    owner, name = rec.repo.split("/", 1)
    if rec.kind == "commit":
        data = gh.get(f"/repos/{owner}/{name}/commits/{rec.commit_sha}").json()
        return (data.get("commit") or {}).get("message") or ""
    if rec.kind == "issue_comment":
        return (
            gh.get(f"/repos/{owner}/{name}/issues/comments/{rec.comment_id}").json().get("body")
            or ""
        )
    if rec.kind == "review_comment":
        return (
            gh.get(f"/repos/{owner}/{name}/pulls/comments/{rec.comment_id}").json().get("body")
            or ""
        )
    number = rec.pr_number or rec.issue_number
    return gh.get(f"/repos/{owner}/{name}/issues/{number}").json().get("body") or ""


def _load_manifest(path: Path) -> list[ManifestRecord]:
    records: list[ManifestRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("_manifest_version"):
                continue
            records.append(ManifestRecord(**row))
    return records


def cmd_hydrate(args: argparse.Namespace) -> int:
    """Fetch bodies into the gitignored cache. This directory is never committed."""
    records = _load_manifest(args.manifest)
    cache_dir = args.cache
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / ".gitignore").write_text("*\n", encoding="utf-8")  # belt and braces

    gh = GitHub(timeout=args.timeout)
    fetched = skipped = mismatched = 0
    try:
        for rec in records:
            dest = _cache_path(cache_dir, rec)
            if dest.exists() and not args.force:
                skipped += 1
                continue
            body = scrub_emails(_fetch_body(gh, rec))
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if digest != rec.sha256_of_body:
                # Not fatal: GitHub bodies are editable, so drift is expected and is
                # itself a finding. Record it rather than pretending the corpus is frozen.
                mismatched += 1
                print(f"  body changed since manifest: {rec.url}", file=sys.stderr)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(body, encoding="utf-8")
            fetched += 1
    finally:
        gh.close()

    print(
        f"fetched {fetched}, skipped {skipped} (already cached), {mismatched} edited since manifest"
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    records = _load_manifest(args.manifest)
    missing = drifted = ok = 0
    for rec in records:
        dest = _cache_path(args.cache, rec)
        if not dest.exists():
            missing += 1
            continue
        digest = hashlib.sha256(dest.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        if digest == rec.sha256_of_body:
            ok += 1
        else:
            drifted += 1
    print(f"verify: {ok} match, {drifted} drifted, {missing} not cached (of {len(records)})")
    return 0 if missing == 0 else 1


# --------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--timeout", type=float, default=30.0)
    sub = p.add_subparsers(dest="cmd", required=True)

    default_manifest = Path("corpus/manifest.jsonl")
    default_cache = settings.corpus_path / "bodies"

    m = sub.add_parser("manifest", help="build the committed ID-only manifest")
    m.add_argument("--out", type=Path, default=default_manifest)
    m.add_argument("--since", type=str, default=None, help="ISO date, e.g. 2023-01-01")
    m.add_argument("--limit", type=int, default=None, help="max records per kind")
    m.add_argument(
        "--kinds",
        nargs="+",
        choices=["issues", "issue_comments", "review_comments", "commits"],
        default=None,
    )
    m.set_defaults(func=cmd_manifest)

    h = sub.add_parser("hydrate", help="fetch bodies into the gitignored runtime cache")
    h.add_argument("--manifest", type=Path, default=default_manifest)
    h.add_argument("--cache", type=Path, default=default_cache)
    h.add_argument("--force", action="store_true")
    h.set_defaults(func=cmd_hydrate)

    v = sub.add_parser("verify", help="re-check cached bodies against manifest hashes")
    v.add_argument("--manifest", type=Path, default=default_manifest)
    v.add_argument("--cache", type=Path, default=default_cache)
    v.set_defaults(func=cmd_verify)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

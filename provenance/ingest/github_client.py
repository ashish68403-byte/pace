"""One authenticated GitHub REST client: pages, rate limits and a request count.

Slice B4 (docs/tasks/eval-harness/004_github_backfill.md) fetches the pool's PRs one at a
time. Everything about talking to GitHub lives here, so the backfill says only what it asks:

* every request sent is counted -- that count is the request budget a run reports;
* the primary limit is never hit: at zero remaining requests the client waits for the reset
  GitHub announced, instead of drawing a refusal;
* a secondary-limit refusal (403 or 429) waits `retry-after`, else backs off; a 5xx or a
  dropped connection backs off; each then retries, at most MAX_ATTEMPTS times in all;
* the token is a SecretStr, and no message or exception carries it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final

import httpx
from pydantic import SecretStr

__all__ = [
    "GITHUB_API",
    "MAX_ATTEMPTS",
    "PER_PAGE",
    "GitHubClient",
    "GitHubError",
    "NotFound",
    "RequestStats",
    "TokenRejected",
]

GITHUB_API: Final = "https://api.github.com"
USER_AGENT: Final = "pace-research/0.1 (final-year project; open-access output)"
PER_PAGE: Final = 100
MAX_ATTEMPTS: Final = 6


class GitHubError(RuntimeError):
    """GitHub refused the request, or kept failing: the run stops."""


class TokenRejected(GitHubError):
    """401: the token is wrong, expired or revoked."""


class NotFound(GitHubError):
    """404 or 410: GitHub does not know this resource, or it is gone."""

    def __init__(self, path: str, status: int) -> None:
        super().__init__(f"GET {path}: {status}")
        self.path = path
        self.status = status


@dataclass
class RequestStats:
    """What a run spent. `requests` counts every request sent, retries included."""

    requests: int = 0
    rate_limit_waits: int = 0
    retries: int = 0
    seconds_waited: float = 0.0


class GitHubClient:
    """GET-only, sequential, counted."""

    def __init__(
        self,
        token: SecretStr,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        timeout: float = 30.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=GITHUB_API,
            transport=transport,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {token.get_secret_value()}",
                "User-Agent": USER_AGENT,
            },
        )
        self._sleep = sleep
        self._clock = clock
        self._remaining: int | None = None
        self._reset_at: float | None = None
        self.stats = RequestStats()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_json(self, path: str) -> Any:
        """One resource. NotFound on 404 or 410; GitHubError on any other refusal."""
        return self._checked(path, self._send(path, None)).json()

    def get_pages(self, path: str) -> list[Any]:
        """Every item of a list, following GitHub's `Link: <...>; rel="next"`."""
        items: list[Any] = []
        url: str | None = path
        params: dict[str, Any] | None = {"per_page": PER_PAGE}
        while url is not None:
            response = self._checked(path, self._send(url, params))
            items.extend(response.json())
            url = response.links.get("next", {}).get("url")
            params = None  # the next link carries its own query string
        return items

    @staticmethod
    def _checked(path: str, response: httpx.Response) -> httpx.Response:
        if response.status_code in (404, 410):
            raise NotFound(path, response.status_code)
        if response.status_code >= 400:
            raise GitHubError(f"GET {path}: {response.status_code}")
        return response

    def _send(self, url: str, params: dict[str, Any] | None) -> httpx.Response:
        for attempt in range(MAX_ATTEMPTS):
            self._wait_for_reset()
            self.stats.requests += 1
            try:
                response = self._client.get(url, params=params)
            except httpx.TransportError:
                self._wait(float(2**attempt), rate_limited=False)
                continue
            self._remember(response)
            if response.status_code == 401:
                raise TokenRejected(
                    "GitHub rejected PACE_GITHUB_TOKEN (401): it is wrong, expired or "
                    "revoked. Create a new one and update .env."
                )
            if _rate_limited(response):
                if self._remaining != 0:  # a secondary limit: there is no reset to wait for
                    retry_after = response.headers.get("retry-after")
                    delay = float(retry_after) if retry_after else float(2**attempt)
                    self._wait(delay, rate_limited=True)
                continue  # a primary limit: _wait_for_reset waits for the reset
            if response.status_code >= 500:
                self._wait(float(2**attempt), rate_limited=False)
                continue
            return response
        raise GitHubError(f"GET {url}: still failing after {MAX_ATTEMPTS} attempts")

    def _remember(self, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is not None and reset is not None:
            self._remaining = int(remaining)
            self._reset_at = float(reset)

    def _wait_for_reset(self) -> None:
        if self._remaining == 0 and self._reset_at is not None:
            self._wait(max(self._reset_at - self._clock(), 0.0) + 1.0, rate_limited=True)
            self._remaining = None

    def _wait(self, seconds: float, *, rate_limited: bool) -> None:
        self._sleep(seconds)
        self.stats.seconds_waited += seconds
        if rate_limited:
            self.stats.rate_limit_waits += 1
        else:
            self.stats.retries += 1


def _rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    spent = response.headers.get("x-ratelimit-remaining") == "0"
    return spent or "rate limit" in response.text.lower()

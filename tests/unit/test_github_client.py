"""The GitHub client: pages, rate limits, the request count, and a token that never leaks."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from provenance.ingest.github_client import (
    MAX_ATTEMPTS,
    GitHubClient,
    GitHubError,
    NotFound,
    TokenRejected,
)

TOKEN = "github_pat_test_0123456789"
PLENTY = {"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "9999999999"}


def make(handler):
    """A client over a fake transport, with a fake clock that sleeping advances."""
    now = {"t": 1_000.0}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    client = GitHubClient(
        SecretStr(TOKEN),
        transport=httpx.MockTransport(handler),
        sleep=sleep,
        clock=lambda: now["t"],
    )
    return client, slept


def test_pages_follow_the_link_header_and_every_request_is_counted():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"id": 3}], headers=PLENTY)
        nxt = '<https://api.github.com/repos/o/r/pulls/1/comments?per_page=100&page=2>; rel="next"'
        return httpx.Response(200, json=[{"id": 1}, {"id": 2}], headers={**PLENTY, "link": nxt})

    client, _ = make(handler)
    assert [c["id"] for c in client.get_pages("/repos/o/r/pulls/1/comments")] == [1, 2, 3]
    assert seen[0] == {"per_page": "100"}
    assert client.stats.requests == 2


def test_a_spent_budget_waits_for_the_reset_instead_of_drawing_a_refusal():
    spent = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1060"}
    responses = iter(
        [httpx.Response(200, json={}, headers=spent), httpx.Response(200, json={}, headers=PLENTY)]
    )
    client, slept = make(lambda request: next(responses))
    client.get_json("/a")
    client.get_json("/b")
    assert slept == [61.0]  # reset at 1060, clock at 1000, plus one second of margin
    assert (client.stats.requests, client.stats.rate_limit_waits) == (2, 1)


def test_a_secondary_limit_waits_retry_after_then_retries():
    responses = iter(
        [
            httpx.Response(429, headers={"retry-after": "7"}),
            httpx.Response(200, json={"ok": True}, headers=PLENTY),
        ]
    )
    client, slept = make(lambda request: next(responses))
    assert client.get_json("/a") == {"ok": True}
    assert slept == [7.0]
    assert (client.stats.requests, client.stats.rate_limit_waits) == (2, 1)


def test_a_server_error_backs_off_and_retries():
    responses = iter([httpx.Response(503), httpx.Response(200, json=[], headers=PLENTY)])
    client, slept = make(lambda request: next(responses))
    assert client.get_json("/a") == []
    assert slept == [1.0]
    assert client.stats.retries == 1


def test_it_gives_up_after_a_bounded_number_of_attempts():
    client, _ = make(lambda request: httpx.Response(503))
    with pytest.raises(GitHubError, match="still failing"):
        client.get_json("/a")
    assert client.stats.requests == MAX_ATTEMPTS


def test_a_rejected_token_stops_the_run_without_revealing_it():
    client, _ = make(lambda request: httpx.Response(401))
    with pytest.raises(TokenRejected) as caught:
        client.get_json("/a")
    assert TOKEN not in str(caught.value)
    assert "PACE_GITHUB_TOKEN" in str(caught.value)


def test_not_found_and_gone_are_their_own_outcome():
    for status in (404, 410):
        client, _ = make(lambda request, status=status: httpx.Response(status, headers=PLENTY))
        with pytest.raises(NotFound) as caught:
            client.get_json("/repos/o/r/pulls/12")
        assert caught.value.status == status


def test_the_token_travels_only_in_the_authorization_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        return httpx.Response(200, json={}, headers=PLENTY)

    client, _ = make(handler)
    client.get_json("/a")

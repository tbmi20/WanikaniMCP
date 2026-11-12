import httpx
import pytest

from conftest import FAKE_TOKEN, FakeWaniKani
from wanikani_mcp.client import API_REVISION, MissingTokenError, WaniKaniClient, WaniKaniError


def make_client(transport, **kwargs) -> WaniKaniClient:
    return WaniKaniClient(FAKE_TOKEN, transport=transport, **kwargs)


async def test_sends_auth_and_revision_headers(fake_api: FakeWaniKani):
    async with make_client(fake_api.transport()) as client:
        await client.get("/user")
    request = fake_api.requests[0]
    assert request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert request.headers["Wanikani-Revision"] == API_REVISION


async def test_follows_next_url_across_pages(fake_api: FakeWaniKani):
    async with make_client(fake_api.transport()) as client:
        subjects = await client.get_collection("/subjects")
    assert len(subjects) == 26
    assert len({s["id"] for s in subjects}) == 26  # no duplicates across pages
    assert len(fake_api.requests) == 3  # 10 + 10 + 6
    assert "page_after_id" in str(fake_api.requests[1].url)


async def test_list_params_are_comma_joined(fake_api: FakeWaniKani):
    async with make_client(fake_api.transport()) as client:
        stats = await client.get_collection("/review_statistics", {"subject_ids": [1052, 1354], "hidden": False})
    assert {s["data"]["subject_id"] for s in stats} == {1052, 1354}
    assert fake_api.requests[0].url.params["hidden"] == "false"


async def test_429_waits_until_ratelimit_reset_then_retries(no_sleep):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, headers={"RateLimit-Reset": "1012"}, text="Rate Limit Exceeded")
        return httpx.Response(200, json={"object": "user", "data": {"level": 5}})

    client = make_client(httpx.MockTransport(handler), sleep=no_sleep, clock=lambda: 1000.0)
    body = await client.get("/user")
    assert body["data"]["level"] == 5
    assert len(calls) == 3
    assert no_sleep.waits == [13.0, 13.0]  # reset - now + 1s margin
    assert client.retries == 2


async def test_429_without_reset_header_uses_exponential_backoff(no_sleep):
    responses = iter([httpx.Response(429), httpx.Response(503), httpx.Response(200, json={"ok": True})])
    client = make_client(httpx.MockTransport(lambda r: next(responses)), sleep=no_sleep)
    assert await client.get("/user") == {"ok": True}
    assert no_sleep.waits == [1.0, 2.0]


async def test_gives_up_after_max_retries(no_sleep):
    client = make_client(httpx.MockTransport(lambda r: httpx.Response(429)), sleep=no_sleep, max_retries=2)
    with pytest.raises(WaniKaniError, match="429"):
        await client.get("/user")
    assert len(no_sleep.waits) == 2


async def test_errors_and_repr_never_contain_token():
    client = make_client(httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "Unauthorized"})))
    with pytest.raises(WaniKaniError) as exc:
        await client.get("/user")
    assert FAKE_TOKEN not in str(exc.value)
    assert FAKE_TOKEN not in repr(client)


def test_missing_token(monkeypatch):
    monkeypatch.delenv("WANIKANI_API_TOKEN", raising=False)
    with pytest.raises(MissingTokenError):
        WaniKaniClient()

"""Thin async client for the WaniKani API v2.

Handles auth headers, pagination (``pages.next_url``) and rate limiting (HTTP 429,
using the ``RateLimit-Reset`` header). The API token is read from the
``WANIKANI_API_TOKEN`` environment variable and is never logged, printed or stored.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

BASE_URL = "https://api.wanikani.com/v2"
API_REVISION = "20170710"
TOKEN_ENV_VAR = "WANIKANI_API_TOKEN"

RETRYABLE_STATUS = {429, 502, 503, 504}


class WaniKaniError(Exception):
    """Raised for API errors. Messages never include the token."""


class MissingTokenError(WaniKaniError):
    def __init__(self) -> None:
        super().__init__(f"Set the {TOKEN_ENV_VAR} environment variable to your WaniKani API token.")


class WaniKaniClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 5,
        max_wait: float = 65.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        token = token or os.environ.get(TOKEN_ENV_VAR)
        if not token:
            raise MissingTokenError()
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Wanikani-Revision": API_REVISION,
                "User-Agent": "wanikani-mcp",
            },
        )
        self._max_retries = max_retries
        self._max_wait = max_wait
        self._sleep = sleep
        self._clock = clock
        self.requests_made = 0
        self.retries = 0

    def __repr__(self) -> str:  # never expose the token
        return f"WaniKaniClient(base_url={str(self._http.base_url)!r})"

    async def __aenter__(self) -> WaniKaniClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _retry_wait(self, response: httpx.Response, attempt: int) -> float:
        reset = response.headers.get("RateLimit-Reset")
        if response.status_code == 429 and reset:
            try:
                wait = float(reset) - self._clock() + 1.0
            except ValueError:
                wait = 2.0**attempt
        else:
            wait = 2.0**attempt
        return min(max(wait, 1.0), self._max_wait)

    async def request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            self.requests_made += 1
            response = await self._http.request(method, url, **kwargs)
            if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                self.retries += 1
                await self._sleep(self._retry_wait(response, attempt))
                continue
            if response.is_error:
                raise WaniKaniError(_describe_error(response))
            return response.json()
        raise AssertionError("unreachable")  # pragma: no cover

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("GET", path, params=_clean(params))

    async def get_collection(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Fetch every page of a collection endpoint and return the resources."""
        items: list[dict[str, Any]] = []
        body = await self.get(path, params)
        while True:
            items.extend(body.get("data", []))
            next_url = (body.get("pages") or {}).get("next_url")
            if not next_url:
                return items
            body = await self.request("GET", next_url)

    async def create_study_material(self, subject_id: int, **fields: Any) -> dict[str, Any]:
        payload = {"study_material": {"subject_id": subject_id, **fields}}
        return await self.request("POST", "/study_materials", json=payload)

    async def update_study_material(self, study_material_id: int, **fields: Any) -> dict[str, Any]:
        payload = {"study_material": fields}
        return await self.request("PUT", f"/study_materials/{study_material_id}", json=payload)


def _clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            value = ",".join(str(v) for v in value)
        elif isinstance(value, bool):
            value = str(value).lower()
        out[key] = value
    return out


def _describe_error(response: httpx.Response) -> str:
    detail = ""
    try:
        detail = str(response.json().get("error", ""))
    except ValueError:
        detail = response.text[:200]
    hint = {
        401: "the API token was rejected",
        403: "the token lacks permission for this action",
        404: "not found",
        422: "WaniKani rejected the request",
        429: "rate limit exceeded after retries",
    }.get(response.status_code, "request failed")
    return f"WaniKani API {response.status_code} on {response.request.method} {response.request.url.path}: {hint}. {detail}".strip()

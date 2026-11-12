"""Shared test helpers: recorded fixtures served through a fake WaniKani API.

No test makes a live network call: every client is built on ``httpx.MockTransport``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_TOKEN = "test-token-not-real"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeWaniKani:
    """Serves the recorded fixtures, split into pages of ``per_page`` records.

    ``updated_after`` filters records by ``data_updated_at``, like the real API.
    Every request is kept in ``self.requests`` so tests can assert on them.
    """

    COLLECTIONS = ("subjects", "assignments", "review_statistics", "study_materials")

    def __init__(self, per_page: int = 10) -> None:
        self.per_page = per_page
        self.data = {name: load(name) for name in self.COLLECTIONS}
        self.user = load("user")
        self.requests: list[httpx.Request] = []
        self.writes: list[tuple[str, str, dict[str, Any]]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v2").strip("/")
        if request.method != "GET":
            return self._write(request, path)
        if path == "user":
            return httpx.Response(200, json=self.user)
        if path not in self.COLLECTIONS:
            return httpx.Response(404, json={"error": "Not found", "code": 404})
        return httpx.Response(200, json=self._page(request, path))

    def _page(self, request: httpx.Request, name: str) -> dict[str, Any]:
        params = request.url.params
        records = sorted(self.data[name]["data"], key=lambda r: r["id"])
        if after := params.get("updated_after"):
            records = [r for r in records if r["data_updated_at"] > after]
        if ids := params.get("subject_ids"):
            wanted = {int(i) for i in ids.split(",")}
            records = [r for r in records if r["data"]["subject_id"] in wanted]
        if params.get("immediately_available_for_review") == "true":
            records = [r for r in records if self.is_due(r)]
        if after_id := params.get("page_after_id"):
            records = [r for r in records if r["id"] > int(after_id)]
        page, rest = records[: self.per_page], records[self.per_page :]
        next_url = None
        if rest:
            query = dict(params)
            query["page_after_id"] = str(page[-1]["id"])
            next_url = str(httpx.URL(f"https://api.wanikani.com/v2/{name}", params=query))
        return {
            "object": "collection",
            "url": str(request.url),
            "pages": {"per_page": self.per_page, "next_url": next_url, "previous_url": None},
            "total_count": len(records),
            "data_updated_at": "2026-11-12T00:00:00.000000Z",
            "data": copy.deepcopy(page),
        }

    NOW = "2026-11-12T12:00:00.000000Z"

    def is_due(self, assignment: dict[str, Any]) -> bool:
        d = assignment["data"]
        return bool(d["available_at"]) and d["available_at"] <= self.NOW and not d["burned_at"]

    def _review(self, body: dict[str, Any]) -> httpx.Response:
        fields = body["review"]
        for record in self.data["assignments"]["data"]:
            if record["id"] == fields["assignment_id"]:
                break
        else:
            return httpx.Response(404, json={"error": "Not found", "code": 404})
        if not self.is_due(record):
            return httpx.Response(422, json={"error": "Assignment is not available for review", "code": 422})
        start = record["data"]["srs_stage"]
        wrong = fields["incorrect_meaning_answers"] + fields["incorrect_reading_answers"]
        end = min(start + 1, 9) if not wrong else max(1, start - -(-wrong // 2) * (2 if start >= 5 else 1))
        record["data"].update(srs_stage=end, available_at="2026-11-20T12:00:00.000000Z")
        record["data_updated_at"] = self.NOW
        return httpx.Response(201, json={
            "id": 0, "object": "review",
            "data": {"assignment_id": record["id"], "subject_id": record["data"]["subject_id"],
                     "starting_srs_stage": start, "ending_srs_stage": end, **fields},
            "resources_updated": {"assignment": copy.deepcopy(record), "review_statistic": None},
        })

    def _write(self, request: httpx.Request, path: str) -> httpx.Response:
        body = json.loads(request.content)
        self.writes.append((request.method, path, body))
        if request.method == "POST" and path == "reviews":
            return self._review(body)
        fields = body["study_material"]
        if request.method == "POST" and path == "study_materials":
            new_id = 99_000_001
            record = {
                "id": new_id,
                "object": "study_material",
                "data_updated_at": "2026-11-12T12:00:00.000000Z",
                "data": {"subject_type": "kanji", "meaning_synonyms": [], "hidden": False,
                         "meaning_note": None, "reading_note": None, **fields},
            }
            self.data["study_materials"]["data"].append(record)
            return httpx.Response(201, json=record)
        if request.method == "PUT" and path.startswith("study_materials/"):
            sm_id = int(path.split("/")[1])
            for record in self.data["study_materials"]["data"]:
                if record["id"] == sm_id:
                    record["data"].update(fields)
                    return httpx.Response(200, json=record)
        return httpx.Response(404, json={"error": "Not found", "code": 404})


@pytest.fixture
def fake_api() -> FakeWaniKani:
    return FakeWaniKani()


@pytest.fixture
def no_sleep():
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    sleep.waits = waits  # type: ignore[attr-defined]
    return sleep


@pytest.fixture
async def client(fake_api: FakeWaniKani, no_sleep):
    from wanikani_mcp.client import WaniKaniClient

    async with WaniKaniClient(FAKE_TOKEN, transport=fake_api.transport(), sleep=no_sleep) as c:
        yield c


@pytest.fixture
def db():
    from wanikani_mcp.db import Database

    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
async def first_sync_summary(client, db):
    from wanikani_mcp.sync import sync

    return await sync(client, db)


@pytest.fixture
async def synced_db(db, first_sync_summary):
    return db

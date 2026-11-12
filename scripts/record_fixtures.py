"""Record a small, redacted set of real API responses for the test suite.

Usage:
    WANIKANI_API_TOKEN=... uv run python scripts/record_fixtures.py 440 449 2467 ...

Fetches the given subject ids (plus their components and visually similar kanji),
and the matching assignments, review statistics and study materials. Personal
fields (username, profile URL, user id, note text) are replaced before writing.
Only GET requests are made.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from wanikani_mcp.client import WaniKaniClient

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def redact_user(body: dict[str, Any]) -> dict[str, Any]:
    data = body["data"]
    data["id"] = "00000000-0000-0000-0000-000000000000"
    data["username"] = "fixture_user"
    data["profile_url"] = "https://www.wanikani.com/users/fixture_user"
    data["started_at"] = "2020-01-01T00:00:00.000000Z"
    body["url"] = "https://api.wanikani.com/v2/user"
    return body


def redact_collection(body: dict[str, Any]) -> dict[str, Any]:
    body["pages"]["next_url"] = None
    body["pages"]["previous_url"] = None
    for item in body["data"]:
        data = item["data"]
        for field in ("meaning_note", "reading_note"):
            if data.get(field):
                data[field] = f"(redacted {field.replace('_', ' ')})"
    return body


async def main(seed_ids: list[int]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with WaniKaniClient() as client:
        user = redact_user(await client.get("/user"))
        seed = await client.get("/subjects", {"ids": seed_ids})
        related: set[int] = set(seed_ids)
        for item in seed["data"]:
            data = item["data"]
            related.update(data.get("component_subject_ids", []))
            related.update(data.get("visually_similar_subject_ids", []))
        ids = sorted(related)
        subjects = redact_collection(await client.get("/subjects", {"ids": ids}))
        assignments = redact_collection(await client.get("/assignments", {"subject_ids": ids}))
        stats = redact_collection(await client.get("/review_statistics", {"subject_ids": ids}))
        materials = redact_collection(await client.get("/study_materials", {"subject_ids": ids}))

    for name, body in {
        "user": user,
        "subjects": subjects,
        "assignments": assignments,
        "review_statistics": stats,
        "study_materials": materials,
    }.items():
        (OUT / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False, indent=1) + "\n")
        count = len(body["data"]) if isinstance(body["data"], list) else 1
        print(f"wrote {name}.json ({count} records)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main([int(x) for x in sys.argv[1:]]))

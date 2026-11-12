"""Pull WaniKani data into the local cache and report what changed.

The first sync fetches everything. Later syncs pass ``updated_after`` (the newest
``data_updated_at`` seen per endpoint) so only changed records are fetched. Each
sync saves a dated snapshot of the leech list and overall accuracy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .client import WaniKaniClient
from .db import Database
from .leeches import HAS_READING, LeechConfig, current_leeches

ENDPOINTS = ("subjects", "assignments", "review_statistics", "study_materials")


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


async def sync(client: WaniKaniClient, db: Database, config: LeechConfig = LeechConfig()) -> dict[str, Any]:
    started = utcnow()
    full = not db.has_synced()
    requests_before = client.requests_made
    old_stages = {row[0]: row[1] for row in db.query("SELECT subject_id, srs_stage FROM assignments")}
    previous_leeches = _last_snapshot_ids(db)

    user = (await client.get("/user"))["data"]
    db.set_user(user["level"], (user.get("subscription") or {}).get("max_level_granted"), started)

    changes: dict[str, dict[str, int]] = {}
    for endpoint in ENDPOINTS:
        cursor = db.get_sync_cursor(endpoint)
        records = await client.get_collection(f"/{endpoint}", {"updated_after": cursor} if cursor else None)
        existing = db.existing_ids(endpoint)
        new = sum(1 for r in records if r["id"] not in existing)
        getattr(db, f"upsert_{endpoint}")(records)
        if records:
            newest = max(r["data_updated_at"] for r in records)
            db.set_sync_cursor(endpoint, max(newest, cursor or ""))
        elif cursor is None:
            db.set_sync_cursor(endpoint, "1970-01-01T00:00:00Z")
        changes[endpoint] = {"new": new, "updated": len(records) - new}

    srs = _srs_movement(db, old_stages) if not full else None
    leeches = current_leeches(db, config)
    run_id = db.conn.execute(
        "INSERT INTO sync_runs (started_at, finished_at, full_sync, requests) VALUES (?, ?, ?, ?)",
        (started, utcnow(), int(full), client.requests_made - requests_before),
    ).lastrowid
    _snapshot(db, run_id, started, leeches)

    leech_ids = {item["subject_id"] for item in leeches}
    summary = {
        "full_sync": full,
        "level": user["level"],
        "changes": changes,
        "srs_movement": srs,
        "leeches": {
            "count": len(leeches),
            "new_since_last_sync": len(leech_ids - previous_leeches) if previous_leeches is not None else None,
            "resolved_since_last_sync": len(previous_leeches - leech_ids) if previous_leeches is not None else None,
        },
        "accuracy": _accuracy(db)["accuracy"],
        "api_requests": client.requests_made - requests_before,
        "rate_limit_retries": client.retries,
    }
    db.conn.execute("UPDATE sync_runs SET summary = ? WHERE id = ?", (json.dumps(summary), run_id))
    db.conn.commit()
    return summary


def _last_snapshot_ids(db: Database) -> set[int] | None:
    last = db.scalar("SELECT MAX(id) FROM sync_runs")
    if last is None:
        return None
    return {r[0] for r in db.query("SELECT subject_id FROM leech_snapshots WHERE sync_run_id = ?", (last,))}


def _srs_movement(db: Database, old_stages: dict[int, int]) -> dict[str, int]:
    moved = {"up": 0, "down": 0, "burned": 0, "newly_started": 0}
    for subject_id, stage in db.query("SELECT subject_id, srs_stage FROM assignments"):
        old = old_stages.get(subject_id)
        if old is None or old == 0:
            if stage > 0:
                moved["newly_started"] += 1
        elif stage > old:
            moved["up"] += 1
            if stage == 9:
                moved["burned"] += 1
        elif stage < old:
            moved["down"] += 1
    return moved


def _accuracy(db: Database) -> dict[str, Any]:
    """Overall answer accuracy. Readings only count for kanji and vocabulary, because
    the API copies the meaning numbers into the reading fields for other types."""
    reading_types = ",".join(f"'{t}'" for t in sorted(HAS_READING))
    row = db.one(f"""
        SELECT COUNT(*) AS items,
               COALESCE(SUM(meaning_correct + CASE WHEN subject_type IN ({reading_types}) THEN reading_correct ELSE 0 END), 0) AS correct,
               COALESCE(SUM(meaning_incorrect + CASE WHEN subject_type IN ({reading_types}) THEN reading_incorrect ELSE 0 END), 0) AS incorrect
        FROM review_statistics WHERE hidden = 0
    """)  # noqa: S608 - constant list
    total = row["correct"] + row["incorrect"]
    return {
        "items": row["items"],
        "correct": row["correct"],
        "incorrect": row["incorrect"],
        "accuracy": round(row["correct"] / total, 4) if total else None,
    }


def _snapshot(db: Database, run_id: int, taken_at: str, leeches: list[dict[str, Any]]) -> None:
    db.conn.executemany(
        """INSERT INTO leech_snapshots
           (sync_run_id, taken_at, subject_id, subject_type, characters, meaning_score, reading_score, score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                run_id, taken_at, item["subject_id"], item["type"], item["characters"], item["meaning"]["score"],
                item["reading"]["score"] if item["reading"] else None, item["score"],
            )
            for item in leeches
        ],
    )
    acc = _accuracy(db)
    db.conn.execute(
        "INSERT INTO accuracy_snapshots (sync_run_id, taken_at, items, correct, incorrect, leech_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, taken_at, acc["items"], acc["correct"], acc["incorrect"], len(leeches)),
    )
